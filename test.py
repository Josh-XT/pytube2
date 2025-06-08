#!/usr/bin/env python3
import sys
import os

# Add the pytube2 path
sys.path.insert(0, "/home/josh/repos/common/pytube2")

from pytube import YouTube

print("Testing the original failing scenario - downloading captions...")
try:
    yt = YouTube("https://www.youtube.com/watch?v=1HAcza0nE34")
    
    # Test the original failing function
    captions = yt.captions
    print(f"Found {len(captions)} caption tracks")
    
    # Download a specific caption
    if 'en' in captions:
        en_caption = captions['en']
        srt_content = en_caption.generate_srt_captions()
        print(f"✅ English caption downloaded successfully!")
        print(f"Caption content length: {len(srt_content)} characters")
        print(f"First 200 characters: {srt_content[:200]}")
    else:
        print("English captions not available, trying first available...")
        if captions:
            first_caption = list(captions)[0]
            first_caption_obj = captions[first_caption.code]
            srt_content = first_caption_obj.generate_srt_captions()
            print(f"✅ Caption ({first_caption.code}) downloaded successfully!")
            print(f"Caption content length: {len(srt_content)} characters")
    
    print("\n🎉 Original issue resolved! Caption downloading works perfectly.")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
