"""Alexa device config for Christy dashboard.

Device discovery was done via browser session to alexa.amazon.com.
TTS/announcements require Home Assistant integration (future).

The dashboard fetches device list from alexa.amazon.com directly
via the user's browser session (no server-side auth needed for GET).
"""

# Known Alexa devices (discovered from Amazon account)
ALEXA_DEVICES = [
    {"serial": "G8M11W1002960ASN", "name": "Office dot", "type": "A1RABVCI4QCIKC", "family": "ECHO", "online": True},
    {"serial": "G8M0XG1111240FDR", "name": "Gym dot", "type": "A1RABVCI4QCIKC", "family": "ECHO", "online": True},
    {"serial": "G090XG0994970RBF", "name": "KitchenDot", "type": "A1RABVCI4QCIKC", "family": "ECHO", "online": True},
    {"serial": "G2A0U204941609A9", "name": "Dongchul's Echo", "type": "A18O6U1UQFJ0XK", "family": "ECHO", "online": True},
    {"serial": "G2A0P30874921352", "name": "Sehee's Echo", "type": "A7WXQPH584YP", "family": "ECHO", "online": False},
    {"serial": "G6G0XG110375063A", "name": "Sehee's Echo Dot", "type": "A1RABVCI4QCIKC", "family": "ECHO", "online": True},
]

CUSTOMER_ID = "A10I92LG4OTH43"
