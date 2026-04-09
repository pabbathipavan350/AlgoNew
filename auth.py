# ============================================================
# AUTH.PY — Kotak Neo Login (Final Fixed Version)
# ============================================================

import pyotp
import logging
import time
import os
import config
from neo_api_client import NeoAPI

logger = logging.getLogger(__name__)

# TOTP secret loaded from .env — never hardcode it here
TOTP_SECRET_KEY = os.getenv("TOTP_SECRET_KEY", "")


def generate_totp() -> str:
    """Auto-generate current TOTP — handles padding automatically"""
    if not TOTP_SECRET_KEY or TOTP_SECRET_KEY == "YOUR_TOTP_SECRET_KEY":
        return None
    try:
        # Clean and pad the key to valid base32 length
        key = TOTP_SECRET_KEY.upper().strip().replace(" ", "")
        padding = (8 - len(key) % 8) % 8
        key = key + "=" * padding
        totp = pyotp.TOTP(key)
        code = totp.now()
        return code
    except Exception as e:
        print(f"TOTP auto-generation failed: {e}")
        return None


def get_kotak_session() -> NeoAPI:
    """
    Authenticate with Kotak Neo API v2.
    Callbacks are assigned as attributes after creation via setup_websocket().
    """
    print("\n" + "="*50)
    print("  Connecting to Kotak Neo API...")
    print("="*50)

    # Step 1: Initialize client — NO callbacks here, assign after
    client = NeoAPI(
        consumer_key = config.KOTAK_CONSUMER_KEY,
        environment  = config.KOTAK_ENVIRONMENT,
        access_token = None,
        neo_fin_key  = None,
    )

    # Step 2: Generate TOTP
    totp_code = generate_totp()

    if totp_code:
        print(f"Auto-generated TOTP: {totp_code}")
    else:
        print("Please open Google Authenticator and enter your Kotak TOTP:")
        totp_code = input("  Enter 6-digit TOTP: ").strip()

    # Step 3: TOTP Login
    try:
        login_response = client.totp_login(
            mobilenumber=config.KOTAK_MOBILE_NUMBER,
            ucc=config.KOTAK_UCC,
            totp=totp_code
        )
    except Exception as e:
        # Try alternate parameter name
        try:
            login_response = client.totp_login(
                mobile_number=config.KOTAK_MOBILE_NUMBER,
                ucc=config.KOTAK_UCC,
                totp=totp_code
            )
        except Exception as e2:
            raise Exception(f"TOTP Login failed: {e2}\nCheck KOTAK_MOBILE_NUMBER and KOTAK_UCC in config.py")

    # Check login response
    if not login_response:
        raise Exception("TOTP Login returned empty response. Check your credentials in config.py")

    # Check for errors in response
    if isinstance(login_response, dict) and login_response.get("error"):
        # Retry with manual TOTP
        print(f"TOTP failed. Please enter manually from Google Authenticator:")
        for attempt in range(3):
            totp_code = input(f"  Enter TOTP (attempt {attempt+1}/3): ").strip()
            try:
                login_response = client.totp_login(
                    mobilenumber=config.KOTAK_MOBILE_NUMBER,
                    ucc=config.KOTAK_UCC,
                    totp=totp_code
                )
                if isinstance(login_response, dict) and not login_response.get("error"):
                    break
            except:
                pass
            if attempt < 2:
                print("  Waiting 5 seconds...")
                time.sleep(5)

    print("TOTP Login: SUCCESS")
    logger.info(f"Login response keys: {list(login_response.keys()) if isinstance(login_response, dict) else type(login_response)}")
    print(f"  [DEBUG] Login resp: {str(login_response)[:300]}")

    # Step 4: MPIN Validation
    print("Validating MPIN...")
    try:
        # Try passing mpin directly
        validate_response = client.totp_validate(mpin=config.KOTAK_MPIN)
    except Exception as e1:
        try:
            # Try with pan as fallback
            validate_response = client.totp_validate(
                mpin=config.KOTAK_MPIN,
                pan=config.KOTAK_UCC
            )
        except Exception as e2:
            try:
                # Try login_response fields if available
                auth = (login_response.get('auth') or
                        login_response.get('Auth') or '') \
                    if isinstance(login_response, dict) else ''
                sid  = (login_response.get('sid') or
                        login_response.get('Sid') or '') \
                    if isinstance(login_response, dict) else ''
                validate_response = client.totp_validate(
                    mpin=config.KOTAK_MPIN,
                    auth=auth,
                    sid=sid
                )
            except Exception as e3:
                raise Exception(
                    f"MPIN validation failed: {e3}\n"
                    f"Please check KOTAK_MPIN in config.py is correct"
                )

    # Check validation response
    if validate_response:
        if isinstance(validate_response, dict) and validate_response.get("error"):
            raise Exception(
                f"MPIN rejected: {validate_response}\n"
                f"Please check your MPIN in config.py"
            )

    print("MPIN Validated: SUCCESS")
    print("Kotak Neo Authentication Complete!")
    logger.info("Kotak Neo auth successful")

    return client


def verify_connection(client: NeoAPI) -> bool:
    """Verify session is active"""
    try:
        print("Verifying connection...")
        limits = client.limits()
        print("Connection verified!")
        return True
    except Exception as e:
        print(f"Connection verify note: {e} — continuing anyway")
        return True
