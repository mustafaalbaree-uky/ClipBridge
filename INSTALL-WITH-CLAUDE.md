# Installing ClipBridge voice notes for someone, with Claude

This file is written for an AI coding assistant (Claude Code or similar) that has been asked to install ClipBridge on a person's Mac and teach them to use it. The person may not be a programmer. Follow it top to bottom.

What they are getting: free, private dictation. They press a hotkey, speak, press it again, and a few seconds later the transcript is on their clipboard, ready to paste anywhere. Transcription runs on their own Mac with whisper.cpp and the `large-v3-turbo` model. Nothing is uploaded, there is no account, and it costs nothing.

What they are **not** getting, and you should not set up: the clipboard sync between devices (Supabase), the Windows app, the iPhone Shortcut, the Cloudflare worker, or a Groq key. ClipBridge runs as voice notes only when there is no Supabase config, which is exactly what is wanted here. Do not create a `supabase_url` or `supabase_anon_key` entry.

## How to work with the person

* Run every command yourself. Do not ask them to copy commands into a terminal, except the one Homebrew step below that needs their Mac password.
* Before each step, say in one plain sentence what it does ("Now I am downloading the speech model, about 1.6 GB"). No jargon.
* Tell them clearly when they need to do something with their hands: type their password, click Allow on a permission box, press the hotkey.
* When something fails, fix it yourself if you can. Only bring them in when you need a click or a password.

## 1. Check the Mac

```bash
uname -m                      # arm64 means Apple Silicon (M1 or later)
sw_vers -productVersion
df -h /System/Volumes/Data    # Avail needs to be at least 4 GB
```

* `arm64` is what this is built for. On `x86_64` (an Intel Mac) it still works, but transcription is several times slower; tell the person that before going on.
* If there is less than 4 GB free, stop and tell them.

## 2. Homebrew

Check with `command -v brew || ls /opt/homebrew/bin/brew`. If Homebrew is already there, skip to step 3.

If it is missing, the installer needs the person to type their Mac login password, which you cannot do for them. Tell them that, then have them run it themselves. In Claude Code they can type this into the prompt box, starting with the `!`:

```
! /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Tell them: when it asks for a password, type the password they use to log in to the Mac and press Return. Nothing appears on screen while they type; that is normal. If it asks them to press Return to continue, they press Return. It can take several minutes, and it may also install Apple's Command Line Tools.

When it finishes, put Homebrew on the path for this and future sessions:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
brew --version
```

## 3. Install whisper.cpp and Python

```bash
brew install whisper-cpp python@3.13
command -v whisper-cli && python3.13 --version
```

## 4. Get ClipBridge

```bash
git clone https://github.com/mustafaalbaree-uky/ClipBridge.git ~/ClipBridge
```

## 5. Download the speech model (about 1.6 GB)

```bash
mkdir -p ~/.clipbridge/models
curl -L --fail -o ~/.clipbridge/models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
ls -lh ~/.clipbridge/models/ggml-large-v3-turbo.bin   # about 1.5G to 1.6G
```

If the file is tiny (a few KB), the download failed; delete it and try again.

## 6. Build and install the app

```bash
cd ~/ClipBridge/mac && ./build.sh --install
```

This makes a private Python environment, builds `ClipBridge.app`, copies it to `/Applications`, and opens it. The first build takes a few minutes. "No Apple Development certificate, left ad hoc signed" is expected and fine.

Check it started:

```bash
pgrep -fl ClipBridge.app
cat ~/.clipbridge/mac_debug.log | tail -5
```

The log line should show `'can_record': True` and `'sync': False`. If `can_record` is False, look at the `local_asr` field in the same line: it says what is missing (usually the model file or `whisper-cli`).

If an alert says "Nothing to run", whisper.cpp or the model is missing: redo steps 3 and 5, then open the app again with `open /Applications/ClipBridge.app`.

## 7. First recording, together

Walk the person through this live. Do not skip it: the microphone permission only appears on the first recording.

1. Tell them a small clipboard icon is now in the menu bar at the top right of the screen. On a MacBook with a notch and a crowded menu bar it can be hidden behind the notch; the hotkey works either way.
2. Have them open any app with a text box (Notes, Messages, a browser).
3. Have them press **Control + Option + Space** and say a sentence.
4. The first time, macOS asks whether ClipBridge may use the microphone. They click **Allow**. If they missed it, they turn it on in System Settings > Privacy & Security > Microphone > ClipBridge, then try again.
5. While recording, the menu bar icon becomes a red dot and a small pill appears near the mouse pointer.
6. They press **Control + Option + Space** again to stop. The icon shows an ellipsis while it transcribes. A short note takes a second or two; a three minute note about seven seconds on a recent Mac.
7. When it is done, the pill shows the text and the transcript is on the clipboard. They press **Command + V** in the text box to paste it.

If nothing is pasted, check `~/.clipbridge/mac_debug.log` and the microphone permission.

## 8. Teach them how it works

Explain these in plain words, one at a time, and check they follow. This is the part that matters most, because the clipboard behaviour is what they will notice day to day.

**The basic loop.** Control + Option + Space starts recording, the same keys stop it. It works from any app, including when ClipBridge is not in front. The transcript goes straight onto the clipboard by itself. They paste it with Command + V wherever they want it. Nothing is typed for them; they choose where it goes by pasting.

**It replaces what was on the clipboard.** When a transcript arrives, it takes the clipboard's place. Whatever they had copied before is gone from the clipboard, so if they copied something important, paste it somewhere first.

**Several notes in a row.** They can start a new note while the previous one is still transcribing, or record several before pasting any. The transcripts come out in the order they were recorded, one at a time:

* The first note goes onto the clipboard.
* The next one waits. It does not overwrite the first one.
* As soon as they paste the first one, the next one goes onto the clipboard automatically, ready for the next Command + V.
* The waiting notes show as pills stacked under the pointer, so they can see how many are left.

**Copying something else stops the line.** If a transcript is sitting on the clipboard and they copy something else (Command + C on some text, for example), ClipBridge leaves their copy alone and does not load the next waiting note on top of it. The waiting notes stay in line. To carry on, they click the menu bar icon and choose **Next Note**.

**Next Note** also skips the note currently on the clipboard and loads the next one, if they decide not to paste it.

**Getting an old transcript back.** Click the menu bar icon, then **Recent Notes**. The five most recent notes are listed by day and time, and clicking one puts its transcript back on the clipboard. **Notes Folder** opens the folder where each note's text is saved as a `.txt` file next to its audio.

**Only five are kept.** When a sixth note is saved, the oldest one is deleted, audio and text together. Anything they want to keep, they should paste into a document.

**If a transcription fails.** The recording is not lost: it is saved and retried automatically. **Retry Pending** in the menu tries again straight away.

**Private.** Everything happens on their Mac. No audio or text is sent anywhere.

**Starts by itself.** ClipBridge opens at login. **Open at Login** in the menu turns that off. **Quit** closes it; they can open it again from the Applications folder.

## 9. Optional settings

Settings live in `~/.clipbridge/config.json`. It does not exist by default, and none of it is needed. If the person wants a change, create the file with only the keys they want, for example:

```json
{
  "record_hotkey": "<ctrl>+<alt>+r",
  "whisper_vocabulary": "Names and terms it keeps getting wrong, written the way they should be spelled."
}
```

* `record_hotkey`: a different key combination for recording. Modifiers are `<ctrl>`, `<alt>` (Option), `<cmd>`, `<shift>`.
* `whisper_vocabulary`: a sentence or two of names, places or terms the transcripts misspell. It helps whisper spell them right.

After changing it, quit ClipBridge from its menu and open it again from Applications.

There is also a pin feature (Control + Option + V types the transcript straight into a text field). It needs Accessibility permission and is described in the README. Leave it out of the first lesson; mention it only if they ask for something like it.

## 10. Updating later

```bash
cd ~/ClipBridge && git pull && cd mac && ./build.sh --install
```

After a rebuild, macOS may ask for the microphone permission again, because the app's signature changes with each build. They click Allow as before.

## Troubleshooting

| What they see | What to check |
|---|---|
| Nothing happens on the hotkey | Is it running (`pgrep -fl ClipBridge.app`)? Another app may own Control + Option + Space; change `record_hotkey`. |
| Records but no text | `~/.clipbridge/mac_debug.log`, and the microphone permission. A note recorded with the microphone off transcribes as silence. |
| Notes skip ahead without being pasted | A clipboard history app (Paste, Maccy, Raycast clipboard history, Alfred) reads the clipboard the moment something lands on it, which counts as a paste. Use Recent Notes to get the skipped one back, or pause that app's clipboard history. |
| Menu bar icon missing | Hidden behind the notch or by a menu bar manager. The hotkey still works. |
| "Nothing to run" alert | whisper.cpp or the model file is missing (steps 3 and 5). |
