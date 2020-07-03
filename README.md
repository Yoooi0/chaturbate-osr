**chaturbate-osr.py 0.1.0:**

A small python script that connects to chaturbate chat and moves the OSR based on tips.

**Requirements:**
- python3

**How to use:**
- run `install-requirements.bat` or `pip install -r requirements.txt` to install required modules
- open `chaturbate-osr.py` in notepad and set `devicePort` to correct OSR com port, optionally edit `deviceAxisRange` to tweak OSR range for each axis
- edit **example-settings.json** to configure chaturbate room, stream delay or the tipmenu
- drag and drop the desired settings file (for example the included **example-settings.json**) over `chaturbate-osr.py` to start, or run `python chaturbate-osr.py settings.json` from command line