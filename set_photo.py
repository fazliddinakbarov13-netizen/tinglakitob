import requests

BOT_TOKEN = "8651052608:AAENF6M__mmmOrMpAGSzdSFdjX3wdzRgA7A"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Profil rasmini o'rnatish
logo_path = r"C:\Users\ASUS\.gemini\antigravity\brain\c6e49de0-4d25-40b2-a06b-dcd03817738c\tinglakitob_logo_1776348711289.png"

with open(logo_path, 'rb') as photo:
    r = requests.post(
        f"{API}/setUserProfilePhoto",
        files={"photo": photo}
    )
    print("setUserProfilePhoto:", r.json())
