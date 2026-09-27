# 🎧 Audio Converter Pro

**Audio Converter Pro** is a desktop audio conversion tool for Windows that makes it easy to convert music and audio files into the format you need.

It is designed to be **simple, easy to use, and quality-focused**.

You don't need to know complicated FFmpeg commands. Just:

**Add files → Choose a format → Choose quality → Convert**

---
<img width="960" height="540" alt="image" src="https://github.com/user-attachments/assets/eb0af8c4-7729-4b2f-ba61-e34d9e6e94d3" />
<img width="960" height="540" alt="image" src="https://github.com/user-attachments/assets/d630b228-2fee-460f-a489-13ae3f92abeb" />
<img width="960" height="540" alt="image" src="https://github.com/user-attachments/assets/dd098a57-44f4-4cc7-9243-ab9249a4c69c" />


---

## 🎵 What Can You Do with Audio Converter Pro?

Audio Converter Pro can convert audio to popular formats such as:

**MP3 · FLAC · AAC · M4A · ALAC · WAV · OGG**

For example, you can:

* Convert **FLAC → MP3** for wider device compatibility.
* Convert **WAV → FLAC** to reduce file size while keeping the audio lossless.
* Convert **AAC → M4A** for devices and players that support MPEG-4 audio.
* Convert large music collections in one batch.
* Convert entire folders instead of selecting files one by one.

---

## ✨ Key Features

### 🎧 Quality-Focused Conversion

The app uses format-specific settings instead of applying the same settings to every audio format.

You can choose:

**Auto · 128 kbps · 192 kbps · 256 kbps · 320 kbps**

Lossless formats such as **FLAC** and **ALAC** are also supported.

When **Auto** is selected, the application uses a quality preset appropriate for the selected format.

### 🏷️ Preserve Song Information

When supported by the target format, Audio Converter Pro keeps important information from the original file, such as:

**Title · Artist · Album · Genre · Metadata**

Embedded **cover artwork** is also preserved when supported by the output format.

This helps keep your music library organized after conversion without having to enter the information again.

### 📂 Batch Conversion

You don't have to convert files one at a time.

You can:

**Add multiple files**

or

**Add an entire folder**

The app can also scan subfolders to find supported audio files.

### 🖱️ Drag & Drop

Simply drag files or folders from Windows Explorer and drop them into the application.

It's one of the fastest ways to start a conversion.

### ✅ Verify Output Files

After conversion, Audio Converter Pro checks the generated file to make sure it was created correctly.

The application verifies important information such as:

* Output file existence and file size
* Output codec
* Audio duration
* Number of audio channels

This helps catch incomplete or invalid output files instead of simply assuming that every FFmpeg process completed successfully.

### 📊 Easy Progress Tracking

Each file displays its current status:

**Pending → Converting → Verifying → Completed / Failed**

Every file has its own progress indicator, making it easy to monitor large batch jobs.

### 🗂️ Safe File Handling

When an output file already exists, you can choose:

**Overwrite · Skip · Rename**

This gives you control over how existing files are handled.

---

## 🎼 Supported Output Formats

| Format   | Best For                                                |
| -------- | ------------------------------------------------------- |
| **MP3**  | General music playback and wide compatibility           |
| **FLAC** | Lossless music storage and high-quality music libraries |
| **AAC**  | Efficient lossy audio                                   |
| **M4A**  | Modern devices and media players                        |
| **ALAC** | Lossless audio storage                                  |
| **WAV**  | Audio editing and uncompressed workflows                |
| **OGG**  | Software and platforms that support OGG                 |

---

## 🚀 How to Use

### 1. Add Your Files

Click **Add Files**, add a folder, or drag and drop files directly into the application.

### 2. Choose the Output Format

Select the format you want:

**MP3 / FLAC / AAC / M4A / ALAC / WAV / OGG**

### 3. Choose the Quality

Leave the setting on **Auto** for a format-appropriate quality preset, or manually choose the bitrate and sample rate.

### 4. Choose an Output Folder

Select where you want the converted files to be saved.

### 5. Start Converting

Click:

**▶ Start Convert**

The application will process your files and show the progress directly in the main window.

---

## 🖥️ Simple, Clean Interface

Audio Converter Pro is designed around a straightforward workflow.

The main window gives you quick access to:

**Add Files · Add Folder · Enable / Disable Files · Remove Files · Convert · Cancel**

Each file is displayed as a separate card with its filename, location, file size, progress, and current status.

---

## 💡 Who Is It For?

Audio Converter Pro is useful for anyone who:

* Has a large music collection and needs batch conversion.
* Wants to convert FLAC, WAV, M4A, AAC, or other formats to MP3.
* Wants to store music in a **lossless format** such as FLAC or ALAC.
* Prefers a graphical application instead of command-line tools.
* Wants simple control over output quality and bitrate.

---

## 🔧 Built With

Audio Converter Pro uses:

**Python · CustomTkinter · FFmpeg · FFprobe**

**FFmpeg** handles the actual audio conversion, while **FFprobe** is used to read file information and verify the converted output.

The application can automatically find FFmpeg and FFprobe through the system `PATH`, or from locations next to the application.

---

## 📋 Requirements

* Windows
* Python
* FFmpeg
* FFprobe
* CustomTkinter
* `tkinterdnd2` *(optional, for Drag & Drop support)*

Install CustomTkinter:

```bash
pip install customtkinter
```

For Drag & Drop support:

```bash
pip install tkinterdnd2
```

Make sure `ffmpeg` and `ffprobe` are available in your system `PATH`, or place them next to the application.

---

## 🚀 Run from Source

```bash
python "Audio Convert Pro.py"
```

Then:

**Add files or folders → Choose a format → Choose quality → Select an output folder → Start Convert**

---

## 📌 Version

**Audio Converter Pro v2.3.1**

A desktop audio conversion tool focused on:

**Simplicity · Quality · Batch Processing · Safe File Handling · Reliability**

---

## ❤️ Project Goal

Audio Converter Pro was built with a simple goal:

> **Make audio conversion easier without requiring users to understand complex technical settings.**

Choose your files, choose your format, and let the application handle the rest.
