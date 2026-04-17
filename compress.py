import os
from moviepy.editor import VideoFileClip

input_path = r"C:\Users\ASUS\Downloads\bot uchun.mp4"
output_path = r"C:\Users\ASUS\Downloads\bot_uchun_Telegram_Format.mp4"

print("Videoni siqish (kompressiya) boshlandi... (Bir oz vaqt olishi mumkin, kuting!)")

try:
    # Videoni ochamiz
    clip = VideoFileClip(input_path)
    
    # Olamiz va o'lchamini optimal qilamiz (agar balandligi 720 dan katta bo'lsa, kichkina qilamiz)
    if clip.h > 720:
        clip = clip.resize(height=720)
        
    # Telegram profili ucin faqat video ketadi (audio kerak emas), ovozni olib tashlaymiz
    if clip.audio is not None:
        clip = clip.without_audio()
        
    # Sifatini vizual buzmaydigan, lekin hajmini kichraytiruvchi sozlamalar
    clip.write_videofile(
        output_path,
        codec="libx264",
        bitrate="500k",      # Hajmni juda kichraytiradi
        preset="slow",        # Sifatni maksimal ushlab turadi
        logger=None
    )
    
    print("\n✅ Muvaffaqiyatli yakunlandi!")
    print(f"Yangi fayl saqlandi: {output_path}")
    print(f"Hajmi: {os.path.getsize(output_path) / (1024 * 1024):.2f} MB")
    
except Exception as e:
    print("\nXato yuz berdi:", e)
