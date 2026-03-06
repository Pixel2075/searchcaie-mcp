import sys
import json
sys.path.insert(0, ".")
from mcp_server import get_questions

ids = [
    17870, 23305, 19771, 20039, 19705, 16058, 18196, 22322, 22370, 21369,
    18772, 21412, 17162, 23362, 18159, 21421, 21126, 19430, 20082, 19704
]

print(f"Calling get_questions with {len(ids)} ids, detail='full'\n")
result = get_questions(question_ids_list=ids, detail='full')

print("--- TEXT OUTPUT (What you see in chat) ---")
print(result.content)
print("\n--- STRUCTURED OUTPUT (What LLM sees) ---")
structured = result.structured_content
print(f"Total questions in structured_content: {len(structured['questions'])}")
if len(structured['questions']) > 0:
    print("Keys in first structured question:", list(structured['questions'][0].keys()))
