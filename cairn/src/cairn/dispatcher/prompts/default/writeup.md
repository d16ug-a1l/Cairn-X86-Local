# Task
You are given the successful solution path of a completed CTF / penetration-testing challenge: the ordered chain of confirmed facts and intents that led from Origin to Goal, plus — when available — the execution records (operation notes and shell commands) collected from the agent sessions that produced each step.

Write a **writeup** of this challenge. The writeup is a reproduction document: a reader must be able to redo the entire solve from scratch by following it, step by step, with nothing else.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

Normal return example:
```json
{"accepted": true, "data": {"writeup": "...(markdown)..."}}
```

# Writeup Rules
- Write the writeup in Chinese (Simplified). Keep commands, payloads, file names, and technical identifiers verbatim in their original form.
- Describe **only the successful path** below. Do not mention failed attempts, dead ends, or off-path exploration.
- Structure: start with a brief challenge overview (target, goal), then the steps in reproduction order, then a final conclusion with the flag / final proof.
- For each step include, in order:
  1. **目的** — why this step is taken (what it establishes or unlocks for later steps).
  2. **执行的命令** — the exact commands, complete and in execution order, in fenced code blocks. Use the real values from the materials (target IPs/URLs, ports, usernames, passwords, file paths). Never abbreviate a command with `...`.
  3. **发送的请求** — when a step issues HTTP/API requests (e.g. via curl, scripts, or tools), spell out the method, URL, key headers/parameters, and payload.
  4. **关键结果** — the important output or observation, including extracted credentials, flags, or artifacts.
- Reproduce commands faithfully from the execution records when they exist. When execution records are missing for a step, reconstruct the commands from the fact chain and say so implicitly by writing them as the confirmed actions — but never invent commands, outputs, or values that contradict the given materials.
- If the materials contain long data referenced from a file path, keep the file reference so the reader can locate it.
- The `writeup` value is a single Markdown string.

# Context
## Project
{project_title}

## Origin
```
{origin}
```

## Goal
```
{goal}
```

## Successful Path (ordered steps from Origin to Goal)
{main_chain}

## Execution Records per Step
{execution_details}
