import os
import numpy as np

# Numpy 2.0 mosligi uchun tuzatish (moviepy eski versiyasi xato bermasligi uchun)
if not hasattr(np.ndarray, 'tostring'):
    np.ndarray.tostring = np.ndarray.tobytes

from moviepy.editor import VideoFileClip

input_path = r"C:\Users\ASUS\Downloads\bot uchun.mp4"
gif_output = r"C:\Users\ASUS\Downloads\bot_uchun_Telegram.gif"

print("Maxsus Telegram o'lchamida GIF tayyorlanmoqda (640x360)...")

try:
    clip = VideoFileClip(input_path)
    
    # Telegram so'ragan aniq o'lchamga keltiramiz: 640x360
    clip = clip.resize(newsize=(640, 360))
    
    # GIF qilib saqlaymiz
    clip.write_gif(
        gif_output,
        fps=12,
        program='imageio',
        opt='nq', 
        logger=None
    )
    
    print("\n✅ GIF tayyor bo'ldi!")
    print(f"Yangi fayl saqlandi: {gif_output}")
    print(f"Hajmi: {os.path.getsize(gif_output) / (1024 * 1024):.2f} MB")
    
except Exception as e:
    print("\nXato yuz berdi:", e)
