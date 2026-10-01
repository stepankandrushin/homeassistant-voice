#!/usr/bin/env python3
import sys
import os
from utils import stt
import config

def main():
    """
    Send one audio file to the transcription server and print the text.

    Usage:
        python transcribe.py <audio_file_path>

    Example:
        python transcribe.py recordings/2025/02/27/mic_20250227_101030.wav
    """
    # Check if a file path was provided
    if len(sys.argv) < 2:
        print("Error: Please provide the path to an audio file.")
        print(f"Usage: python {sys.argv[0]} <audio_file_path>")
        sys.exit(1)

    # Get the file path from command line arguments
    audio_file = sys.argv[1]

    # Check if the file exists
    if not os.path.exists(audio_file):
        print(f"Error: File not found: {audio_file}")
        sys.exit(1)

    print(f"Transcribing file: {audio_file}")
    print(f"Using transcription server at: {config.TRANSCRIPTION_API_URL}")
    transcript = stt.transcribe(audio_file)

    # Display the result
    if transcript:
        print("\nTranscription result:")
        print("-" * 40)
        print(transcript)
        print("-" * 40)
    else:
        print("Transcription failed.")
        sys.exit(1)

if __name__ == "__main__":
    main()
