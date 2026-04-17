import shutil

source = r"C:\Users\ASUS\.gemini\antigravity\brain\c6e49de0-4d25-40b2-a06b-dcd03817738c\bot_description_pic_1776349882974.png"
dest = r"c:\Users\ASUS\Desktop\ANTIGRAVITYDAGI ISHLARIM\tinglakitob\Bot_Kirish_Rasmi.png"

try:
    shutil.copy(source, dest)
    print("Muvaffaqiyatli saqlandi!")
except Exception as e:
    print("Xato yuz berdi:", e)
