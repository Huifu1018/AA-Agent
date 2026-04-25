# Gmail Attachment Reply

## Purpose

Use this skill when AA handles unread Gmail messages and needs to decide whether to reply, especially when the message itself, the message attachments, or the local material knowledge base are relevant to the answer.

This skill describes the Gmail watchdog flow that is already wired into this project.

## End-to-end workflow

For each newly detected unread Gmail message:

1. Read sender, subject, preview, and the latest email body only
2. Apply hard no-reply rules first
3. Detect whether the email has Gmail attachments
4. Extract text from supported Gmail attachments when possible
5. Optionally consult the local material knowledge base when:
   - the email asks about material already prepared locally
   - the answer can be improved by citing local materials
   - the sender explicitly asks for documents, reports, PPTs, or attachments
6. Decide whether a reply is needed
7. If a reply is needed, generate a final sendable plain-text reply
8. Send the reply with reply-all semantics
9. Attach local files only when the email clearly requests them
10. Mark the original Gmail message as read

## Hard rules

### 1. Only use the latest email body

Do not reason over the full thread by default.

When the email body contains quoted history, forwarded content, or thread headers, trim at common markers such as:

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

### 2. Hard no-reply rule

Messages from the `Yuan-lab-LLM/ClawManager` notification flow must not be replied to automatically.

Current project rule:

- if sender or subject contains `[Yuan-lab-LLM/ClawManager]`
- set `need_reply = no`
- record the reason and stop before reply generation

### 3. Attachment-first reasoning

If the email has Gmail attachments and the task depends on them, do not send a placeholder reply.

Bad examples:

- "我先查看附件，稍后回复。"
- "我会尽快整理后反馈。"
- "收到，我先看看附件。"

Required behavior:

- If attachment text is available, use it to produce a final response now
- If attachment text is not available, say that the attachment could not be read and ask for readable text or key data

### 4. Supported Gmail attachment types

Prefer extracting text from:

- `.txt`
- `.csv`
- `.pdf`
- `.docx`
- `.xlsx`

If parsing fails, record the parsing failure and avoid pretending the attachment was understood.

### 5. Local material knowledge base

This project also supports a local material knowledge base generated from user-specified folders.

The knowledge base is stored as a Markdown catalog and includes, per document:

- path
- type
- tags
- summary
- excerpt
- applicable scenes
- forbidden scenes
- keywords

Use this catalog when:

- the email asks for material that may already exist locally
- the answer should cite or summarize a local document
- the sender asks for a document to be attached

The catalog can be used in two ways:

- `use_catalog`: use catalog entries as extra context for answering
- `should_attach`: actually attach selected local files to the reply

Do not attach local files unless the email clearly asks for materials or attachments.

### 6. Final reply required

If the model judges `need_reply = yes`, it must also provide a usable final reply body.

If the first assessment returns `need_reply = yes` but the reply body is empty or unusable:

- run a second-pass reply generation step
- generate a directly sendable final reply
- only then continue to send

Do not leave the workflow in a half-finished state where the system knows a reply is needed but sends nothing.

### 7. Plain-text reply only

The reply body must be normal email text:

- no Markdown headings
- no `**bold**`
- no `-` or `*` bullet lists unless absolutely unavoidable
- prefer short paragraphs and natural business-email phrasing

If the model still emits Markdown formatting, sanitize it before sending.

### 8. Reply all

When sending the reply:

- reply to the original sender
- include original CC recipients
- exclude the authenticated mailbox itself
- exclude duplicate addresses

If local files are selected, attach them to the same reply-all message.

### 9. Quote the previous email

The sent reply should include:

- the new reply content at the top
- the previous email quoted below
- proper threading headers such as `In-Reply-To` and `References`

The quoted block is for email threading and context only.
The newly generated reply at the top should remain plain text.

### 10. Read state

After handling the message:

- if processing completes, mark the original email as read
- do not leave already processed emails unread

## Failure policy

If any of the following are true:

- attachment content is required but cannot be extracted
- local catalog selection crashes or returns unusable results
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
- Gmail attachment filenames
- Gmail attachment text summary
- whether local catalog was used
- selected local attachment paths
- local attachment selection reason
- need_reply
- reason
- final reply text
- sent reply id
- sent reply subject
- marked_read

## Current project behavior

This skill reflects the current Gmail watchdog behavior in this project:

- only the latest email body is used for reasoning
- Gmail attachment text is merged into the reasoning context when available
- local material catalog entries can also be merged into the reasoning context
- local files are only attached when the email clearly asks for them
- replies use reply-all semantics
- replies are sent directly, not just saved as drafts
- processed messages are marked as read
- messages from `[Yuan-lab-LLM/ClawManager]` are skipped by hard rule

## Notes for future skills

This skill should remain a workflow description for the real, shipping Gmail behavior in this project:

- keep workflow-specific logic explicit
- separate extraction rules from response rules
- prefer safe failure over shallow replies
- document hard no-reply rules and attachment policies clearly
