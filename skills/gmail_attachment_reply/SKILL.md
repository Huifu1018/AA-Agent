# Gmail Attachment Reply

## Purpose

Use this skill when Agent-S handles new Gmail messages and needs to decide whether to reply, especially when the email includes attachments.

This skill is designed for the project's Gmail watchdog flow.

## Goal

For each newly detected unread Gmail message:

1. Read sender, subject, preview, and latest email body only
2. Detect whether the email contains attachments
3. Extract text from supported attachments when possible
4. Decide whether a reply is needed
5. If a reply is needed, generate a final reply based on:
   - the latest email body
   - attachment contents
6. Send the reply directly with "reply all" semantics
7. Mark the original message as read

## Rules

### 1. Only use the latest email body

Do not reason over the full thread by default.

When the email body contains quoted history, headers, or forwarded content, trim at common markers such as:

- `发件人:`
- `收件人:`
- `抄送:`
- `主题:`
- `From:`
- `To:`
- `Cc:`
- `Subject:`
- `On ... wrote:`
- `Original Message`

The model should reason over the newest email content, not older quoted messages.

### 2. Attachment-first reasoning

If the email has attachments and the task requested in the email depends on those attachments, do not send a placeholder reply.

Bad examples:

- "我先查看附件，稍后回复。"
- "我会尽快整理后反馈。"
- "收到，我先看看附件。"

Required behavior:

- If attachment text is available, use it to produce a final response
- If attachment text is not available, explicitly state that the attachment could not be read and ask for readable text or key data

### 3. Supported attachment types

Prefer extracting text from:

- `.txt`
- `.csv`
- `.pdf`
- `.docx`
- `.xlsx`

If parsing fails, record the parsing failure and avoid pretending the attachment was understood.

### 4. Reply quality

When a reply is required:

- The reply should be directly sendable
- The reply should be concise but complete
- The reply should answer the user's actual request
- If the email asks for analysis based on an attachment, the reply must reflect the attachment content
- Do not stop at "need_reply=yes"; the workflow must produce actual reply text and send it

### 4.1 Final reply required

If the model judges `need_reply = yes`, it must also provide a usable final reply body.

If the first assessment returns `need_reply = yes` but the reply body is empty or unusable:

- run a second-pass reply generation step
- generate a directly sendable final reply
- only then continue to send

Do not leave the workflow in a half-finished state where the system knows a reply is needed but sends nothing.

### 4.2 Plain-text reply only

The reply body must be normal email text:

- no Markdown headings
- no `**bold**`
- no `-` or `*` bullet lists unless absolutely unavoidable
- prefer short paragraphs and natural business-email phrasing

If the model still emits Markdown formatting, sanitize it before sending.

### 5. Reply all

When sending the reply:

- Reply to the original sender
- Include original CC recipients
- Exclude the authenticated mailbox itself
- Exclude duplicate addresses

If local files are selected, attach them to the same reply-all message.

### 6. Quote the previous email

The sent reply should include:

- the new reply content at the top
- the previous email quoted below
- proper threading headers such as `In-Reply-To` and `References`

The quoted block is for email threading and context only.
The newly generated reply at the top should remain plain text and should not contain Markdown formatting.

### 7. Read state

After handling the message:

- If processing completes, mark the original email as read
- Do not leave already processed emails unread

## Failure policy

If any of the following are true:

- attachment content is required but cannot be extracted
- the sender or body is malformed
- Gmail permissions are insufficient

Then:

- do not fabricate a content-based answer
- record the reason
- fail safely or send a transparent limitation message only when appropriate

If attachment content is successfully available:

- do not send a vague acknowledgment
- do not say "I will review the attachment later"
- do not send a holding reply

Instead:

- produce the final answer based on the attachment content now

## Recommended output fields

When recording processing results, include:

- sender
- subject
- date
- latest body
- attachment filenames
- attachment text summary
- need_reply
- reason
- final reply text
- sent reply id
- sent reply subject
- marked_read

## Current project behavior

This skill reflects the current Gmail watchdog behavior in this project:

- only the latest email body is used for reasoning
- attachment text is merged into the reasoning context when available
- replies use reply-all semantics
- replies are sent directly, not just saved as drafts
- processed messages are marked as read
- UI should show current unread-focused information rather than stale already-read history

## Notes for future skills

This skill is intended to be a template for future workflow skills in this project:

- keep workflow-specific logic explicit
- separate extraction rules from response rules
- prefer safe failure over shallow replies
