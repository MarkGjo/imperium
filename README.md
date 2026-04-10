Imperium

A text-based AI agent that lets you control your Mac from your phone. Send natural language commands and watch your computer execute them in real-time.

What is Imperium?

Imperium is a personal automation system that bridges your phone and your Mac. Instead of walking to your computer, just text it what you want — open apps, send emails, play music, search the web, or even create entire projects.

The system uses a FastAPI backend running on your Mac that interprets natural language commands and translates them into AppleScript, shell commands, or direct app integrations. A simple web frontend accessible from your phone acts as the remote control.

Why We Built This

We wanted to eliminate the friction between thinking of something and doing it on a computer. Whether you're on the couch, in another room, or just don't want to context-switch — you can control your Mac with a quick text.

No need to remember complex commands or navigate through menus. Just say what you want in plain English:

- "Open YouTube and search for MrBeast"
- "Send an email to john@example.com about the meeting tomorrow"
- "Play Drake on Spotify"
- "Create a calculator app"
- "Commit changes in MyProject with message fixed the bug"


Demo Link

https://youtu.be/jEpbP-iGQTk

Features

- App Control — Open, close, and switch between any Mac application
- Web Navigation — Open websites, search Google/YouTube, navigate to specific pages
- Email Composition — Compose and send emails with visual typing so you can watch it happen
- Spotify Integration — Play songs, control playback, search for music
- iMessage — Send texts to contacts by name or phone number
- Git Operations — Commit, push, pull, and check status of your repos
- Project Generation — Describe an app and watch it get created
- Visual Typing — See text being typed character-by-character for emails under 1000 characters
- Live Navigation Bar — See what app/page is currently active on your phone

Setup

1. Clone the repo
2. Install dependencies: `pip install -r backend/requirements.txt`
3. Add your Anthropic API key to `backend/.env`
4. Run the server: `python backend/main.py`
5. Open `http://YOUR_MAC_IP:8000/app` on your phone

The Future

This is just the beginning. Here's where Imperium can go:

- Voice Control — Add speech-to-text so you can speak commands instead of typing
- Multi-Device Support — Control multiple Macs from a single interface
- Automation Chains — Create workflows that execute multiple commands in sequence
- Screen Awareness — Let the AI see your screen and make context-aware decisions
- Calendar & Task Integration — "Schedule a meeting with John tomorrow at 3pm"
- File Management — "Move all PDFs from Downloads to Documents"
- Smart Home Bridge — Control your lights, thermostat, and devices through your Mac
- Custom Plugins — Let users add their own integrations and commands
- Learning Mode — Watch how you use your Mac and suggest automations

The goal is simple: your computer should work for you, not the other way around.

---

Built with Claude AI, FastAPI, and AppleScript.
