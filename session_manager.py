# ============================================================
# SESSION_MANAGER.PY — Kotak Session Keepalive
# ============================================================
# FIX #5: Kotak Neo sessions expire during the day.
# This runs a background thread that:
#   1. Pings the API every 25 minutes with a lightweight call
#   2. If ping fails → attempts re-login automatically
#   3. Logs all session events
#
# Without this: session expires silently → orders fail → 
# position open with no monitoring → loss
# ============================================================

import threading
import time
import datetime
import logging

logger = logging.getLogger(__name__)


class SessionManager:

    def __init__(self, client, auth_fn):
        """
        client  : authenticated NeoAPI client
        auth_fn : function to re-authenticate (get_kotak_session)
        """
        self.client       = client
        self.auth_fn      = auth_fn
        self.is_running   = True
        self.last_ping    = datetime.datetime.now()
        self.ping_ok      = True
        self._thread      = None
        self.on_reconnect = None   # callback when session refreshed

    def start(self):
        """Start background keepalive thread."""
        self._thread = threading.Thread(
            target  = self._keepalive_loop,
            daemon  = True,
            name    = "SessionKeepalive"
        )
        self._thread.start()
        print("[Session] Keepalive started — pinging every 25 mins")

    def stop(self):
        self.is_running = False

    def _keepalive_loop(self):
        """Background loop — ping every 25 minutes."""
        PING_INTERVAL = 25 * 60   # 25 minutes in seconds

        while self.is_running:
            time.sleep(PING_INTERVAL)

            if not self.is_running:
                break

            now = datetime.datetime.now()
            t   = now.time()

            # Only ping during market hours
            if t < datetime.time(9, 0) or t > datetime.time(15, 35):
                continue

            self._ping()

    def _ping(self):
        """
        Lightweight API call to keep session alive.
        Uses limits() — minimal data, just checks connectivity.
        """
        try:
            resp = self.client.limits(segment="ALL", exchange="NSE", product="ALL")
            self.last_ping = datetime.datetime.now()
            self.ping_ok   = True
            logger.debug(f"Session ping OK at {self.last_ping.strftime('%H:%M')}")
            print(f"  [Session] Keepalive ping OK "
                  f"({self.last_ping.strftime('%H:%M')})")

        except Exception as e:
            self.ping_ok = False
            logger.warning(f"Session ping failed: {e}")
            print(f"\n  [Session] ⚠️  Session may have expired: {e}")
            print(f"  [Session] Attempting re-login...")
            self._relogin()

    def _relogin(self):
        """Re-authenticate and restore session."""
        for attempt in range(3):
            try:
                new_client = self.auth_fn()
                if new_client:
                    self.client   = new_client
                    self.ping_ok  = True
                    print(f"  [Session] ✅ Re-login successful")
                    logger.info("Session re-login successful")
                    # Notify algo to update its client reference
                    if self.on_reconnect:
                        self.on_reconnect(new_client)
                    return
            except Exception as e:
                logger.error(f"Re-login attempt {attempt+1} failed: {e}")
                time.sleep(10)

        print(f"  [Session] ❌ Re-login failed after 3 attempts")
        print(f"  [Session] Orders may fail — check your connection")
        logger.critical("Session re-login failed — manual intervention needed")

    def get_client(self):
        """Always get the latest valid client."""
        return self.client

    @property
    def is_healthy(self):
        return self.ping_ok
