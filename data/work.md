# Work — Notion & Context Reference

> Load this file whenever Xi asks about work-related Notion pages, meeting notes, or FedEx context.

---

## Notion Workspaces

Xi has two Notion workspaces:

| Workspace | Status | Purpose |
|-----------|--------|---------|
| **work** | Active, fully set up | FedEx Dataworks — all work pages, meetings, team info |
| **personal** | May not be fully configured | Personal projects (returned errors on last access) |

---

## Work Workspace: Page Hierarchy

### Top-level pages

| Page | ID | Type |
|------|----|------|
| Team Members | `2f024954-9137-8012-bf6d-e6b6017438bf` | page |
| Data | `33b24954-9137-8067-810d-f1da9a2f2fa5` | page |
| Private | `32c24954-9137-803c-a608-c861063df3ff` | page (Xi's personal section) |

### Private section (under `32c24954`)

| Page | ID | Type |
|------|----|------|
| Responsibility | (subpage) | page |
| Goals Tracker | `33d24954-9137-80df-b909-d151eee9b8dd` | database |
| Study info | `34224954-9137-80ac-8c65-d8a5a967fe2c` | page |
| 📋 Call Transcripts | `36824954-9137-8194-bc68-cb7c38ef1f62` | page |
| └─ 📋 AI Transcript Index | `36d24954-9137-81df-be0f-c48af63c8285` | page (child of Call Transcripts) |

### Team structure (shared workspace area)

```
Company Home (database)
 └─ Enablers Home
     └─ Digital Support Manager
         └─ ☀️ Support Ecosystem dUX Team
             └─ 📝 Meeting Notes  (32824954-9137-8033-875c-ff3ce438c409)
```

**📝 Meeting Notes** contains weekly design review and planning meeting recaps (Design Review I, Design Review II, strategic planning, etc.). Pages named by date.

---

## AI Transcripts: Conventions

- **Location:** Work workspace → Private → 📋 Call Transcripts → 📋 AI Transcript Index
- **Format:** `@Date Time (Timezone)` — e.g. `@May 27, 2026 9:00 AM (GMT+2)`
- **Relative date format:** For recent transcripts, `@Yesterday`, `@Last Monday`, `@Today` with resolved dates
- **Organization:** Index page groups links by month/year headings (## 2024, ## 2025, ## March 2026, etc.)
- **All transcripts are self-created** AI-generated call summaries, not official meeting notes

---

## Personal Workspace

| Page | ID | Type |
|------|----|------|
| To-do | `36f24954-9137-804c-ad80-dd125165f591` | page |

## To-do Convention

When Xi says "add to my to-do list" or "add a task," add the item to the **To-do** page (`36f24954-9137-804c-ad80-dd125165f591`) in the **personal** workspace. Append as paragraphs to this page.

---

## Notion Write Rules (CRITICAL)

Whenever creating, appending, or archiving any Notion page:

1. **Always state explicitly in the response:**
   - Which **workspace** (personal or work)
   - Which **parent page** it goes under (name + ID)
   - The **title** of the page being created/edited

2. **Never leave empty hub pages.** If creating a parent container, it must have content.

3. **All Notion writes are STAGED** — nothing is created until Xi confirms.

4. **Before any search or write,** confirm which workspace is correct for the task.

---

## Key Project Pages

Quick-reference pages to avoid searching blindly:

| Page | ID | Workspace |
|------|----|-----------|
| OCR Capability in MAGIC | `36624954-9137-80c6-8b47-df6ea7565af4` | work |

---

## Xi's Role Context

- **Current title:** Senior Product Manager at FedEx Dataworks
- **Team:** Support Ecosystem dUX Team (under Digital Support Manager, Enablers)
- **Background:** 5 years Data Scientist, 1 year SWE, 2 years DL research, now PM
- **Location:** Netherlands
- **Work patterns:** Strong connector — action items land on Xi across meetings. Passiveness in large cross-functional settings. Sharp in technical discussions.
