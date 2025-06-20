import os
import json
import re
import ast
import requests
import datetime
from dateutil import parser
import streamlit as st
from dotenv import load_dotenv
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

# Load environment variables from .env file
load_dotenv()

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
    return path

def extract_meeting_info(chat_content):
    system = (
        "Extract all candidates with their respective interviewer details (name, email), date, time from the chat. "
        "Return ONLY a single valid JSON object. Output ONLY valid minified JSON. Use double quotes. "
        "Do not include explanations, comments, or markdown."
    )
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

    # Try to extract first { ... } block from LLM response
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        raise ValueError("Could not parse JSON from LLM response. Raw text:\n" + text)
    json_like = m.group(0)

    # Try loading as JSON, fallback to Python dict literal
    try:
        return json.loads(json_like)
    except Exception:
        try:
            safe = (
                json_like
                .replace("None", "null")
                .replace("True", "true")
                .replace("False", "false")
            )
            data = ast.literal_eval(safe)
            return data
        except Exception:
            raise ValueError(f"Failed to parse JSON. Raw extracted string:\n{json_like}")

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
        r.raise_for_status()
    return True

def get_bot_reply(user_input, thread, agent):
    project_client.agents.create_message(
        thread_id=thread.id,
        role="user",
        content=user_input
    )
    project_client.agents.create_and_process_run(
        thread_id=thread.id,
        assistant_id=agent.id
    )
    msgs = project_client.agents.list_messages(thread_id=thread.id)
    bot_msg = next(iter(msgs.text_messages), None)
    reply = bot_msg.text['value'] if bot_msg else "Sorry, I didn't understand that."
    return reply

# ---- STREAMLIT APP ----
st.set_page_config(page_title="INTELLIBOT", page_icon="🤖", layout="centered")
st.markdown(
    """
    <style>
    .stChatMessage {font-size: 1.15rem;}
    .css-1d391kg {background-color: #1e293b !important;}
    .css-18ni7ap {background: #f1f5f9;}
    .st-emotion-cache-1v0mbdj {padding: 2rem 0;}
    .st-emotion-cache-10trblm {font-size: 2.3rem; font-weight: 800; color: #3b82f6;}
    .css-5rimss {background: #e0e7ef;}
    </style>
    """,
    unsafe_allow_html=True,
)
st.markdown("<h1 style='text-align: center;'>🤖 INTELLIBOT</h1>", unsafe_allow_html=True)
st.write("Welcome! Ask any HR hiring or interview scheduling questions in the chat. Type 'exit' to schedule interviews and finish the session.")

if "history" not in st.session_state:
    st.session_state.history = []
if "scheduling_done" not in st.session_state:
    st.session_state.scheduling_done = False
if "scheduling_result" not in st.session_state:
    st.session_state.scheduling_result = ""
if "chat_mode" not in st.session_state:
    st.session_state.chat_mode = True

agent  = project_client.agents.get_agent(AGENT_ID)
thread = project_client.agents.get_thread(THREAD_ID)

# --- Chat window ---
for msg in st.session_state.history:
    with st.chat_message("user"):
        st.markdown(msg["user"])
    with st.chat_message("assistant"):
        st.markdown(msg["bot"])

if st.session_state.chat_mode and not st.session_state.scheduling_done:
    user_input = st.chat_input("Type your message and hit Enter…")
    if user_input:
        if user_input.strip().lower() == "exit":
            st.session_state.chat_mode = False
        else:
            with st.chat_message("user"):
                st.markdown(user_input)
            bot_reply = get_bot_reply(user_input, thread, agent)
            with st.chat_message("assistant"):
                st.markdown(bot_reply)
            st.session_state.history.append({"user": user_input, "bot": bot_reply})

# --- After exit, run scheduling ---
if not st.session_state.chat_mode and not st.session_state.scheduling_done:
    with st.chat_message("assistant"):
        st.info("Thank you! Extracting meeting info and scheduling interviews...")
    chat_file = save_chat_history(st.session_state.history)
    with open(chat_file, "r", encoding="utf-8") as f:
        chat_content = f.read()
    try:
        info = extract_meeting_info(chat_content)
    except Exception as e:
        with st.chat_message("assistant"):
            st.error(f"Extraction failed: {e}")
        st.session_state.scheduling_done = True
        st.stop()
    candidates = info.get('candidates', [])
    if not candidates:
        with st.chat_message("assistant"):
            st.error("No candidates found for scheduling.")
        st.session_state.scheduling_done = True
        st.stop()
    try:
        token = get_access_token()
    except Exception as e:
        with st.chat_message("assistant"):
            st.error(f"Microsoft Graph Auth failed: {e}")
        st.session_state.scheduling_done = True
        st.stop()

    success_count = 0
    out_msgs = []
    for idx, c in enumerate(candidates, 1):
        if CANDIDATE_EMAIL_OVERRIDE:
            c['email'] = CANDIDATE_EMAIL_OVERRIDE
        interviewer = c.get('interviewer')
        if not interviewer:
            out_msgs.append(f"⚠️ Skipping Candidate {idx}: No interviewer data")
            continue
        required_fields = ['name', 'email', 'date', 'time']
        missing = [field for field in required_fields if not c.get(field)]
        if missing:
            out_msgs.append(f"⚠️ Skipping Candidate {idx}: Missing fields {', '.join(missing)}")
            continue
        try:
            success = create_teams_meeting(token, interviewer, c)
            if success:
                out_msgs.append(f"✅ Meeting {idx}: {c['name']} with {interviewer['name']} on {c['date']} at {c['time']} ({c['email']})")
                success_count += 1
        except Exception as err:
            out_msgs.append(f"⚠️ Failed Candidate {idx}: {err}")

    st.session_state.scheduling_result = "\n".join(out_msgs)
    with st.chat_message("assistant"):
        st.success(f"Successfully scheduled {success_count}/{len(candidates)} meetings!")
        for msg in out_msgs:
            st.write(msg)
    st.session_state.scheduling_done = True

# Final summary if already done
if st.session_state.scheduling_done:
    with st.chat_message("assistant"):
        st.write("**Session complete. Reload the app to start new chat.**")

