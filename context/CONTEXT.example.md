# My conventions (injected verbatim into the agent's system prompt — edit freely)

<!--
HOW TO USE THIS FILE
1. Copy it:  context/CONTEXT.example.md  ->  context/CONTEXT.md
2. Replace every <ANGLE-BRACKET PLACEHOLDER> with your own details.
3. Delete lines that don't apply to you. Plain English is fine: the agent
   reads this file like instructions from you.
The app reads context/CONTEXT.md if it exists, otherwise this example file.
context/CONTEXT.md is in .gitignore, so your personal rules stay out of git.
-->

## About me
- Name: <YOUR FIRST NAME>
- Timezone: <YOUR IANA TIMEZONE, e.g. America/Chicago> (must match RING_TZ in .env)

## TickTick

### Default

- If a capture does not clearly match a rule below, put it in **<YOUR DEFAULT LIST NAME>**. This is my main capture list and I sort it later.
- Do not use the built-in TickTick **Inbox**. <!-- delete this line if you DO want the Inbox -->
- Never create new lists or new tags. Only use the ones named here.

### Where things go

<!-- One row per TickTick list. Use the list's exact name (emoji optional:
     lookup ignores emoji, punctuation and capital letters). -->

| List | What goes there |
|---|---|
| <YOUR DEFAULT LIST NAME> | Default for quick tasks, reminders, people to contact, things to read or look up. |
| <LIST NAME, e.g. Home> | <WHAT BELONGS HERE, e.g. chores, errands, things to buy for the house> |
| <LIST NAME, e.g. Work> | <WHAT BELONGS HERE> |
| <LIST NAME, e.g. Projects> | <WHAT BELONGS HERE> |
| <LIST NAME, e.g. Fun> | <WHAT BELONGS HERE, e.g. games, hobbies, side projects> |

### Do not write to these unless I name the list

- <LIST OR FOLDER NAME>

### Never write to these

- <LIST OR FOLDER NAME, e.g. anything in an Archive folder>

### Tags

- Only add a tag if I say it out loud. Existing tags: `<tag-1>`, `<tag-2>`.

### Dates and priority

- If I give a day or time, set it as the due date. If I don't, make it due **today** (all-day, no time).
- If I say "urgent" or "important", set high priority. Otherwise leave priority at none.

## Google Calendar
- Things that happen at a set time (appointments, meetings, plans with
  people) go on my calendar. If no duration is said, make it 1 hour.
- If I say "remind me to..." that's a TickTick task, not an event.

## Notion
<!-- Name the pages/databases you want captures to land in, e.g.
- **Journal** is a database: one row per day.
-->
- General ideas go under the **<IDEAS PAGE NAME, e.g. Ideas>** page. I'll usually say so when
  something belongs elsewhere.
- Unclear captures go to the **<INBOX PAGE NAME, e.g. Inbox>** page.

## Formatting preferences
- Match the page you're adding to. On list-style pages (mostly bullets
  under headings) add a single bullet under the right heading - no title,
  no date.
- On journal/notes-style pages, add a dated entry (YYYY-MM-DD) with a
  short title line, then the content as bullets.

## Needs my approval
Ask me first (request_approval -> Approve/Deny on my phone) before:
- Deleting anything: tasks, calendar events, Notion pages or content.
- Bulk changes: changing, moving or completing more than 3 items in one capture.

Everything else, just do it: creating tasks, events and notes, appending to
pages, editing something you just created, completing a single task.
