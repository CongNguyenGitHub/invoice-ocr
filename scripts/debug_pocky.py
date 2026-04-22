"""Check why the POCKY string failed the mid-question-mark test."""
pn = "BANH QUE POCKY COOKIES ? CREAM 40G"

for word in pn.split():
    core = word.strip(".,;:!()-[]\"'")
    if "?" in core and "???" not in core:
        print("Checking word:", repr(core))
        for i, ch in enumerate(core):
            if ch != "?":
                continue
            left_ok = i > 0 and (core[i-1].isalpha() or core[i-1].isdigit())
            right_ok = i < len(core)-1 and (core[i+1].isalpha() or core[i+1].isdigit())
            print(f" left_ok: {left_ok}, right_ok: {right_ok}")
