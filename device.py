"""
device.py
---------
Handles this device's identity and its registration handshake with
the FastAPI "hydro" backend:
  1. Log in (auth.login()) to get a fresh JWT.
  2. POST the device itself to config.DEVICE_URL (HydroDevice.device_id).
     If the backend already has a row for this device_id (a re-flash
     or a reboot after a previous successful boot), that's NOT an
     error - the existing record's numeric id is looked up instead of
     creating a duplicate.
  3. Bulk-register its actuators to config.ACTUATOR_BULK_URL, tagged
     with that numeric id (the backend's actuator table's device_id
     foreign key is an integer PK, NOT our string device_id/device
     code - confirmed from the 'device_id: 1' seen in /hydro/status
     and the int_parsing validation errors when the string was sent).
  4. If config.DEVICE_LOCATION is set, sync it to the backend once per
     boot via PUT /hydro/devices/{numeric_id} - the POST create path
     (step 2) only sets location on a device's FIRST-EVER registration,
     so for a device that already exists (the common case after boot
     #1), location has to be pushed through the update route instead.     
     
Run once at boot (and retried from main.py if WiFi drops and
recovers), so the backend always has an up-to-date picture of the
device and what it can do.
"""

import ujson
import urequests

import config
import auth

HTTP_TIMEOUT_S = getattr(config, "HTTP_TIMEOUT_S", 8)
DEVICE_LABEL = getattr(config, "DEVICE_MODEL", "esp32-hydro-controller")


class Device:
    def __init__(self, actuator_manager):
        self.device_id = config.DEVICE_CODE   # our string code (HydroDevice.device_id)
        self.numeric_id = None                # backend's integer PK, resolved by register()
        self.actuator_manager = actuator_manager
        self.registered = False
        self._actuators_registered = False    # only bulk-register actuators ONCE per boot
        self._location_synced = False         # only PUT location ONCE per boot

    def register(self, ip_address):
        if not auth.is_authenticated():
            if not auth.login():
                self.registered = False
                return False

        if not self._register_device(ip_address):
            self.registered = False
            return False

        if self.numeric_id is None:
            print("[device] could not resolve a numeric device id, aborting")
            self.registered = False
            return False
        
        # Keep location in sync even on an already-registered device -
        # see module docstring: POST only sets it on first-ever create,
        # so an existing device needs the PUT/update path instead.
        if not self._location_synced:
            self._sync_location()        

        if self._actuators_registered:
            # WiFi reconnect calls register() again to refresh the
            # device row's ip_address, but /actuators/bulk creates new
            # rows on every call rather than upserting - re-running it
            # here would duplicate every actuator again on each flap.
            self.registered = True
            return True

        if not self._register_actuators():
            self.registered = False
            return False

        self._actuators_registered = True
        self.registered = True
        return True

    # ---------------------------------------------------------
    # Device itself: POST /hydro/devices
    # ---------------------------------------------------------
    def _register_device(self, ip_address):
        location = getattr(config, "DEVICE_LOCATION", None)
        payload = {
            "device_id": self.device_id,
            "name": DEVICE_LABEL + " (" + self.device_id + ")",
            "external_id": None,
            "type": "controller",
            "is_active": True,
            "thresholds": None,
            # client_id/ip_address: see module docstring note above.
            "client_id": config.CLIENT_ID,
            "ip_address": ip_address,
        }
        
        # Only include location on the create payload if this board
        # actually has one configured - omitting the key (rather than
        # sending null) avoids ever clobbering a location that might
        # already be set some other way (e.g. dashboard) for a device
        # this firmware doesn't know has one.

        if location:
            payload["location"] = location        
        
        body = ujson.dumps(payload)

        try:
            resp = urequests.post(config.DEVICE_URL, data=body, headers=auth.build_headers(),
                                   timeout=HTTP_TIMEOUT_S)

            if resp.status_code == 401:
                resp.close()
                if not auth.login():
                    return False
                resp = urequests.post(config.DEVICE_URL, data=body, headers=auth.build_headers(),
                                       timeout=HTTP_TIMEOUT_S)

            status = resp.status_code

            if 200 <= status < 300:
                data = resp.json()
                resp.close()
                self.numeric_id = data.get("id")
                print("[device] device registered OK, numeric id =", self.numeric_id)  
                # location (if any) was included in this create payload,
                # so it's already set on the backend - no need for a PUT.
                if location:
                    self._location_synced = True                
                
                return True

            try:
                detail = resp.text
            except Exception:
                detail = "<no body>"
            resp.close()

            if status == 400 and "already exists" in detail.lower():
                # Idempotent case: this board already has a row from a
                # previous boot - not an error, just resolve its id.
                print("[device] already registered, looking up its numeric id")
                return self._lookup_existing_device()

            print("[device] device registration rejected, status %s" % status)
            print("[device] backend said:", detail)
            print("[device] payload sent:", body)
            return False

        except Exception as e:
            print("[device] device registration failed: %s" % e)
            return False

    def _lookup_existing_device(self):
        """GET /hydro/devices and find this board's row by device_id, to recover its numeric id."""
        try:
            resp = urequests.get(config.DEVICE_URL, headers=auth.build_headers(),
                                  timeout=HTTP_TIMEOUT_S)
            if resp.status_code != 200:
                print("[device] device lookup failed, status", resp.status_code)
                resp.close()
                return False

            devices = resp.json()
            resp.close()

            # Response could be a bare list or {"devices": [...]} -
            # handle both defensively, same lesson as /hydro/status.
            if isinstance(devices, dict):
                devices = devices.get("devices", [])

            for d in devices:
                if d.get("device_id") == self.device_id:
                    self.numeric_id = d.get("id")
                    print("[device] resolved existing numeric id =", self.numeric_id)
                    return True

            print("[device] device_id not found in device list")
            return False

        except Exception as e:
            print("[device] device lookup failed: %s" % e)
            return False
        
    # ---------------------------------------------------------
    # Location: PUT /hydro/devices/{numeric_id}
    # ---------------------------------------------------------
    def _sync_location(self):
        """
        Pushes config.DEVICE_LOCATION to the backend via the update
        route. Only runs if a location is actually configured for this
        board, and only once per boot (self._location_synced) - it's
        idempotent server-side, but there's no reason to PUT it on
        every WiFi-reconnect re-registration.
        """
        location = getattr(config, "DEVICE_LOCATION", None)
        if not location:
            self._location_synced = True  # nothing to do, don't keep retrying every register() call
            return True
 
        url = config.DEVICE_URL + "/" + str(self.numeric_id)
        body = ujson.dumps({"location": location})
 
        try:
            resp = urequests.put(url, data=body, headers=auth.build_headers(),
                                  timeout=HTTP_TIMEOUT_S)
 
            if resp.status_code == 401:
                resp.close()
                if not auth.login():
                    return False
                resp = urequests.put(url, data=body, headers=auth.build_headers(),
                                      timeout=HTTP_TIMEOUT_S)
 
            status = resp.status_code
 
            if 200 <= status < 300:
                resp.close()
                print("[device] location synced ->", location)
                self._location_synced = True
                return True
 
            try:
                detail = resp.text
            except Exception:
                detail = "<no body>"
            resp.close()
            print("[device] location sync rejected, status %s" % status)
            print("[device] backend said:", detail)
            # Don't set _location_synced - retry on the next register()
            # call (e.g. next WiFi reconnect) rather than giving up
            # for the rest of this boot.
            return False
 
        except Exception as e:
            print("[device] location sync failed: %s" % e)
            return False        

    # ---------------------------------------------------------
    # Actuators: GET /actuators/device/{id} then POST /actuators/bulk
    # ---------------------------------------------------------
    def _register_actuators(self):
        existing = self._fetch_existing_actuators()

        if existing is None:
            # Couldn't confirm whether actuators already exist - fail
            # closed on the side of NOT creating, rather than risk
            # another round of duplicates. register() will retry the
            # whole handshake next boot/reconnect.
            print("[device] could not confirm actuator status, skipping bulk create for now")
            return False

        if existing:
            # This device already has actuator rows from a previous
            # boot - /actuators/bulk only ever creates (see
            # actuator_router.py: POST /bulk -> create_actuator for
            # each item, no upsert), so calling it again would just
            # pile on more duplicates. Skip - nothing to do.
            print("[device] %d actuators already registered, skipping bulk create" % len(existing))
            return True

        payload = self.actuator_manager.registration_payload(device_id=self.numeric_id)
        return self._post(config.ACTUATOR_BULK_URL, payload, "actuators")

    def _fetch_existing_actuators(self):
        """
        GET /actuators/device/{numeric_id} - what makes registration
        idempotent across reboots. Returns a list (possibly empty) on
        success, or None if the check itself couldn't be completed.
        """
        url = config.ACTUATOR_URL + "/device/" + str(self.numeric_id)
        try:
            resp = urequests.get(url, headers=auth.build_headers(), timeout=HTTP_TIMEOUT_S)

            if resp.status_code == 401:
                resp.close()
                if not auth.login():
                    return None
                resp = urequests.get(url, headers=auth.build_headers(), timeout=HTTP_TIMEOUT_S)

            if resp.status_code != 200:
                resp.close()
                return None

            data = resp.json()
            resp.close()
            return data if isinstance(data, list) else None

        except Exception as e:
            print("[device] existing-actuator lookup failed: %s" % e)
            return None
             
    def _post(self, url, payload, label):
        body = ujson.dumps(payload)
        try:
            resp = urequests.post(url, data=body, headers=auth.build_headers(),
                                   timeout=HTTP_TIMEOUT_S)

            if resp.status_code == 401:
                resp.close()
                if not auth.login():
                    return False
                resp = urequests.post(url, data=body, headers=auth.build_headers(),
                                       timeout=HTTP_TIMEOUT_S)

            ok = 200 <= resp.status_code < 300
            status = resp.status_code

            if ok:
                resp.close()
                print("[device] %s registered OK" % label)
            else:
                try:
                    detail = resp.text
                except Exception:
                    detail = "<no body>"
                resp.close()
                print("[device] %s registration rejected, status %s" % (label, status))
                print("[device] backend said:", detail)
                print("[device] payload sent:", body)

            return ok

        except Exception as e:
            print("[device] %s registration failed: %s" % (label, e))
            return False
