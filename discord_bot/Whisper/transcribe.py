import whisper


def main():
    path = "./harvard.wav"
    audio_path = path

    
    model = whisper.load_model("base") # can scale up to "small", "medium", "large". To be tested
    result = model.transcribe(audio_path, language="en")

    print(result["text"].strip())


if __name__ == "__main__":
    main()
