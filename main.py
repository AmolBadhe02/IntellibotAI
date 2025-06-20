import os
import sys
import json
import re
import requests
import datetime
from dateutil import parser
from dotenv import load_dotenv
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

# Load environment variables from .env file
load_dotenv()

# === Configuration ===
AZURE_CONN_STR           = os.getenv("AZURE_CONN_STR")
AGENT_ID                 = os.getenv("AGENT_ID")
THREAD_ID                = os.getenv("THREAD_ID")
TENANT_ID                = os.getenv("TENANT_ID")
CLIENT_ID                = os.getenv("CLIENT_ID")
CLIENT_SECRET            = os.getenv("CLIENT_SECRET")
USER_EMAIL               = os.getenv("USER_EMAIL")
GROQ_API_KEY             = os.getenv("GROQ_API_KEY")
GROQ_API_URL             = os.getenv("GROQ_API_URL")
MODEL_NAME               = os.getenv("MODEL_NAME")
CANDIDATE_EMAIL_OVERRIDE = os.getenv("CANDIDATE_EMAIL_OVERRIDE")

# === Initialize Azure AI Project client ===
project_client = AIProjectClient.from_connection_string(
    credential=DefaultAzureCredential(),
    conn_str=AZURE_CONN_STR
)

def save_chat_history(history):
    base = "all_chat_history_sr_"
    os.makedirs(base, exist_ok=True)
    files = [f for f in os.listdir(base) if f.endswith(".txt")]
    nums = [int(re.findall(r'\d+', f)[-1]) for f in files if re.search(r'\d+', f)]
    sr = max(nums, default=0) + 1
    path = os.path.join(base, f"all_chat_history_sr_{sr}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Serial Number: {sr}\n\n")
        for e in history:
            f.write(f"User: {e['user']}\nBot: {e['bot']}\n\n" + "-"*40 + "\n\n")
    print(f"💾 Chat history saved as {path}")
    return path

def chatbot_interaction():
    agent  = project_client.agents.get_agent(AGENT_ID)
    thread = project_client.agents.get_thread(THREAD_ID)
    print("Chatbot: Hi! How can I help you today?")
    hist = []

    while True:
        user = input("You: ").strip()
        if user.lower() == "exit":
            print("Chatbot: Exiting and saving chat history…")
            return save_chat_history(hist)

        # 1) Send the user's message
        project_client.agents.create_message(
            thread_id=thread.id,
            role="user",
            content=user
        )

        # 2) Process the agent run (use assistant_id here)
        project_client.agents.create_and_process_run(
            thread_id=thread.id,
            assistant_id=agent.id
        )

        # 3) Fetch the latest assistant reply
        msgs = project_client.agents.list_messages(thread_id=thread.id)
        bot_msg = next(iter(msgs.text_messages), None)
        reply = bot_msg.text['value'] if bot_msg else "Sorry, I didn't understand that."

        print(f"Chatbot: {reply}")
        hist.append({"user": user, "bot": reply})

        # If the agent asks for interviewer details, proceed to scheduling
        if "interviewer name" in reply.lower():
            print("Chatbot: Got the interviewer details, proceeding to scheduling…")
            return save_chat_history(hist)

def extract_meeting_info(chat_content):
    system = "Extract all candidates with their respective interviewer details (name, email), date, time from the chat. Return a single JSON object."
    user = f"""Chat log:
{chat_content}

Return JSON in this shape:
{{
  "candidates": [
    {{
      "name": "Candidate Name",
      "email": "email@example.com",
      "interviewer": {{
        "name": "Interviewer Name",
        "email": "interviewer@example.com"
      }},
      "date": "YYYY-MM-DD",
      "time": "HH:MM AM/PM",
      "product": "Interview"
    }}
  ]
}}"""
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user}
        ],
        "temperature": 0.2
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    resp = requests.post(GROQ_API_URL, json=payload, headers=headers)
    resp.raise_for_status()
    text = resp.json()['choices'][0]['message']['content']
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        raise ValueError("Could not parse JSON from LLM response")
    return json.loads(m.group(0))

def get_access_token():
    url = f'https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token'
    data = {
        'grant_type':    'client_credentials',
        'client_id':     CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scope':         'https://graph.microsoft.com/.default'
    }
    r = requests.post(url, data=data)
    r.raise_for_status()
    return r.json()['access_token']

def create_teams_meeting(token, interviewer, candidate):
    date = candidate.get('date')
    time_str = candidate.get('time')
    if not date or not time_str:
        raise ValueError(f"Missing date or time for candidate {candidate.get('name','Unknown')}")

    dt = parser.parse(f"{date} {time_str}")
    start = dt.isoformat()
    end = (dt + datetime.timedelta(minutes=40)).isoformat()

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "subject": f"Interview: {candidate.get('product','Interview')} with {candidate['name']}",
        "body": {
            "contentType": "HTML",
            "content": (
                f"Dear {candidate['name']},<br><br>"
                f"Your interview for <b>{candidate.get('product','Interview')}</b> with "
                f"{interviewer['name']} has been scheduled."
            )
        },
        "start": {"dateTime": start, "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": end, "timeZone": "Asia/Kolkata"},
        "location": {"displayName": "Microsoft Teams Meeting"},
        "attendees": [
            {"emailAddress": {"address": candidate['email'], "name": candidate['name']}, "type": "required"},
            {"emailAddress": {"address": interviewer['email'], "name": interviewer['name']}, "type": "required"}
        ],
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness"
    }

    url = f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}/events"
    r = requests.post(url, headers=headers, json=payload)
    if not r.ok:
        print(f"⚠️ Graph error {r.status_code}: {r.text}")
        r.raise_for_status()
    return r.json()['onlineMeeting']['joinUrl']

if __name__ == "__main__":
    history_file = chatbot_interaction()
    if not history_file:
        print("❌ No chat history created")
        sys.exit(1)

    with open(history_file, 'r', encoding="utf-8") as f:
        chat = f.read()
    try:
        info = extract_meeting_info(chat)
    except Exception as e:
        print("❌ Extraction failed:", e)
        sys.exit(1)

    candidates = info.get('candidates', [])
    if not candidates:
        print("❌ No candidates found")
        sys.exit(1)

    try:
        token = get_access_token()
    except Exception as e:
        print("❌ Auth failed:", e)
        sys.exit(1)

    # Schedule meetings for valid candidates
    success_count = 0
    for idx, c in enumerate(candidates, 1):
        c['email'] = CANDIDATE_EMAIL_OVERRIDE
        interviewer = c.get('interviewer')

        if not interviewer:
            print(f"⚠️ Skipping Candidate {idx}: No interviewer data")
            continue

        required_fields = ['name', 'email', 'date', 'time']
        missing = [field for field in required_fields if not c.get(field)]
        if missing:
            print(f"⚠️ Skipping Candidate {idx}: Missing fields {', '.join(missing)}")
            continue

        try:
            join_url = create_teams_meeting(token, interviewer, c)
            print(f"✅ Meeting {idx}: {c['name']} with {interviewer['name']}")
            print(f"   Candidate: {c['email']}")
            print(f"   Interviewer: {interviewer['email']}")
            print(f"   Join URL: {join_url}\n")
            success_count += 1
        except Exception as err:
            print(f"⚠️ Failed Candidate {idx}: {err}\n")

    print(f"\n📅 Successfully scheduled {success_count}/{len(candidates)} meetings")
