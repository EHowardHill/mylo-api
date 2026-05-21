final = ""

base = "/var/www/mylo_api/"

file_list = [
    base + "app.py",
    base + "shared_api.py",
    base + "composite.py",
    base + "encryption.py",
    base + "routes/posts.py",
    base + "routes/push.py",
    base + "routes/api.py",
    base + "routes/circles.py",
    base + "routes/users.py",
]

for file in file_list:

    try:
        with open(file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            final += f"{file}:\n{content}\n\n"
    except Exception as e:
        print(f"Skipping {file} (likely not a text file or permission error).")

with open("source.txt", "w", encoding="utf-8") as f:
    f.write(final.strip())
