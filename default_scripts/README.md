# Lab Instructions

## Setup

Before you start coding:

1. Open `config.sh` and make sure your roll number is correct (should already be filled in).
2. Rename the folder `CS2XBXXX` (or similar) to your **actual roll number in ALL CAPS**.
3. Write your solutions in the `.cpp` files inside your roll number folder (`Q1.cpp`, `Q2.cpp`, etc.).

## Testing locally — `check.sh`

This compiles your code and runs it against the **sample test cases** that came with your kit. No server needed.

Run from the lab root directory (where `check.sh` is, not inside your roll number folder):

```bash
# Test all questions at once
./check.sh

# Test only question 1
./check.sh 1

# Test only question 2
./check.sh 2
```

You'll see pass/fail results for each test case and a summary at the end. If a test fails, your actual output is saved in `actual_output/` so you can compare it with the expected output in `testcases/`.

## Submitting to the server — `submit.sh`

This uploads your code to the server for grading against the **hidden private test cases**. Your marks depend on these results.

```bash
# Submit all questions at once
./submit.sh

# Submit only question 1
./submit.sh 1

# Submit only question 3
./submit.sh 3
```

After submitting, the script waits for the server to finish grading and prints your results, marks, and leaderboard rank.

There's a short cooldown between submissions (a few seconds), so don't spam it.

## Tips

- Always run `./check.sh` first to make sure your code compiles and passes the sample cases before submitting.
- You can submit as many times as you want. Only your **best score** is kept — your grade never goes down.
- If you get "Time Limit Exceeded", your code is too slow. Check for infinite loops or inefficient algorithms.
- If you get "Runtime Error", your code crashed (segfault, out-of-bounds access, etc.).
- Back up your code before making big changes. If something breaks, you can always go back.

## Manual testing

If you want to test with your own input:

```bash
cd YOUR_ROLL_NUMBER
g++ Q1.cpp -o Q1
./Q1 < your_input.txt
```

Or just run it interactively and type input manually:

```bash
./Q1
```
