import requests

BOT_TOKEN = "8651052608:AAENF6M__mmmOrMpAGSzdSFdjX3wdzRgA7A"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# 1. Bot nomi
r1 = requests.post(f"{API}/setMyName", json={"name": "TinglaKitob"})
print("setMyName:", r1.json())

# 2. Qisqa tavsif (bot profilida ko'rinadigan matn)
short_desc = "PDF va TXT kitoblarni 8 xil tilda ovozli audioga aylantiraman!"
r2 = requests.post(f"{API}/setMyShortDescription", json={"short_description": short_desc})
print("setMyShortDescription:", r2.json())

# 3. To'liq tavsif (bot haqida ma'lumot)
full_desc = (
    "TinglaKitob — kitoblarni tinglash uchun yaratilgan bot!\n\n"
    "PDF yoki TXT kitob yuboring — men uni 8 xil tilda "
    "(O'zbek, Rus, Ingliz, Turk, Arab, Hind, Xitoy, Koreys) "
    "ovozli audioga aylantirib beraman!\n\n"
    "Ayol va Erkak ovozi tanlash imkoniyati\n"
    "Audio tezligini sozlash\n"
    "Xatcho'p va davom ettirish\n"
    "Referal tizimi — do'stlarni taklif qiling!"
)
r3 = requests.post(f"{API}/setMyDescription", json={"description": full_desc})
print("setMyDescription:", r3.json())

# 4. Bot buyruqlari
commands = [
    {"command": "start", "description": "Botni boshlash"},
    {"command": "kutubxona", "description": "Tayyor kitoblar"},
    {"command": "davom", "description": "Oxirgi joydan davom etish"},
    {"command": "statistika", "description": "Shaxsiy statistika"},
    {"command": "referal", "description": "Do'stlarni taklif qilish"},
]
r4 = requests.post(f"{API}/setMyCommands", json={"commands": commands})
print("setMyCommands:", r4.json())

print("\nBarcha sozlamalar muvaffaqiyatli o'rnatildi!")
