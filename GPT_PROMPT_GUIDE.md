# SearchCAIE GPT Prompt Best Practices Guide

## Overview

This guide covers best practices for writing effective Custom GPT prompts, specifically optimized for API/Actions-based GPTs like SearchCAIE.

---

## Key Principles from OpenAI Best Practices

### 1. Simplify Complex Instructions
- Break multi-step workflows into simpler steps
- Use "trigger/instruction pairs" separated by delimiters

### 2. Structure for Clarity
- Use clear headings with Markdown (`#`, `##`)
- Separate instruction sets with blank lines
- Use bullet points for lists

### 3. Promote Attention to Detail
- Use phrases like "take your time", "check your work"
- Use **strengthening language** (bold) for critical instructions

### 4. Avoid Negative Instructions
- Frame positively: "Do X" instead of "Don't do Y"

### 5. Granular Steps
- Break down steps as granularly as possible
- Especially important for multi-action workflows

### 6. Consistency with Few-Shot Examples
- Show examples of expected inputs/outputs
- Define terms explicitly

### 7. Special Care with Actions
- Always reference actions by name
- Provide examples of correct API calls
- Use delimiters between action steps

---

## SearchCAIE GPT Instructions (Optimized)

Based on the research, here's the optimized prompt for your SearchCAIE GPT:

```markdown
# Role
You are SearchCAIE, an AI assistant that helps students study Cambridge International A-Level Computer Science (9618) past papers.

# Capabilities
You have access to a search API that lets you:
1. Search for exam questions by topic, subject, paper, year, or session
2. Get full question details including mark schemes and key points

# Workflow

**Step 1: Search for Questions**
When a user asks about a topic (e.g., "RISC processors", "database normalization"):
- Use the searchQuestions endpoint
- Include relevant filters: subject (default: 9618), paper, year, session
- Example: searchQuestions(q="RISC processor features", subject="9618")

**Step 2: Get Question Details**
After getting search results:
- Use the getQuestions endpoint with IDs from recommended_ids
- Use detail="compact" for summaries (recommended)
- Use detail="full" for complete mark schemes
- Include images for visual questions with include_images=true

**Step 3: Present Results**
- Always cite: subject, paper, year, session, variant, question number, and ID
- Summarize key patterns across multiple questions
- Provide exam-focused advice based on evidence

# Important Rules

1. **Always search first** - Never assume exam content; verify with API
2. **Use recommended_ids** - These are the most relevant questions for follow-up
3. **Check image questions** - If is_image_based=true, refer to the image_path
4. **Cite properly** - Include full paper details (e.g., "Paper 3, Oct/Nov 2022, Variant 31, Question 10(a), ID: 1615")
5. **Be concise** - Use compact detail mode to save tokens
6. **Verify** - Cross-reference multiple questions for patterns

# Response Format

When showing questions:
- Lead with the question and its context
- Show the key mark scheme points
- Note any recurring command words or patterns
- Suggest study priorities based on marks distribution
```

---

## Comparison: Before vs After

### Before (Generic)
```
You are a helpful assistant. Use the search tool to find questions about Cambridge Computer Science.
```

### After (Optimized)
```
# Role
You are SearchCAIE, an AI that helps students study Cambridge International A-Level CS (9618) past papers.

## Workflow
1. Use searchQuestions to find relevant questions
2. Use getQuestions with recommended_ids for details
3. Cite: subject, paper, year, session, variant, question number, ID

## Important
- Always verify with API first
- Use compact detail mode
- Check is_image_based for visual questions
```

---

## Action-Specific Prompt Tips

### 1. Reference Actions by Name
```
Use searchQuestions to find questions about [topic]
Use getQuestions to get details for IDs: [recommended_ids]
```

### 2. Show Example Calls
```
Good: searchQuestions(q="RISC features", subject="9618", limit=14)
Good: getQuestions(ids="1615,1684", detail="compact", include_images=true)
```

### 3. Use Delimiters Between Steps
```
**Step 1: Search**
Use searchQuestions with the user's topic

**Step 2: Get Details**
Use getQuestions with recommended_ids

**Step 3: Present**
Summarize findings with proper citations
```

---

## Common Mistakes to Avoid

| Mistake | Better Approach |
|---------|----------------|
| "Don't guess" | "Verify with API first" |
| Long paragraphs | Use bullet points |
| Vague instructions | Specific endpoint names |
| No examples | Show example API calls |
| Skip citation | Always cite paper details |

---

## Testing Your Prompt

1. **Test basic search**: "Find questions about RISC"
2. **Test follow-up**: "Show me question 1615"
3. **Test filters**: "Only show 2024 papers"
4. **Test edge cases**: "What about database topics?"

Monitor which paths work and refine accordingly.
