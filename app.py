import os
import json
import re
import requests
import datetime
from dateutil import parser
import streamlit as st
import pandas as pd
from dotenv import load_dotenv
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

# ---- ENVIRONMENT AND CLIENTS ----
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

if "scheduled_events" not in st.session_state:
    st.session_state.scheduled_events = {}

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

def extract_schedule_cancel_info(bot_msg):
    sched = re.search(
        r'✅ Interview scheduled for ([\w\s]+) \(([\w\.-]+@[\w\.-]+)\s*&\s*([\w\.-]+@[\w\.-]+)\)', bot_msg)
    if sched:
        candidate_name = sched.group(1).strip()
        candidate_email = sched.group(2).strip()
        interviewer_email = sched.group(3).strip()
        date_match = re.search(r'on (\d{4}-\d{2}-\d{2})', bot_msg)
        time_match = re.search(r'at ([\d: ]+[APMapm]+)', bot_msg)
        job_profile_match = re.search(r'for ([\w\s\-\(\)\.]+) with', bot_msg)
        date_str = date_match.group(1) if date_match else datetime.date.today().isoformat()
        time_str = time_match.group(1) if time_match else "10:00 AM"
        job_profile = job_profile_match.group(1).strip() if job_profile_match else ""
        interviewer_name = interviewer_email.split('@')[0].replace('.', ' ').title()
        if not job_profile or job_profile.lower() == "interview":
            job_profile = st.session_state.get("last_job_profile", "python developer")
        st.session_state["last_job_profile"] = job_profile
        return {
            "action": "schedule",
            "candidate": {
                "name": candidate_name,
                "email": candidate_email,
                "interviewer": {
                    "name": interviewer_name,
                    "email": interviewer_email
                },
                "date": date_str,
                "time": time_str,
                "job_profile": job_profile
            }
        }
    cancel = re.search(
        r'❌ Interview cancelled for ([\w\s]+) \(([\w\.-]+@[\w\.-]+)\s*&\s*([\w\.-]+@[\w\.-]+)\)', bot_msg)
    if cancel:
        candidate_name = cancel.group(1).strip()
        candidate_email = cancel.group(2).strip()
        interviewer_email = cancel.group(3).strip()
        interviewer_name = interviewer_email.split('@')[0].replace('.', ' ').title()
        return {
            "action": "cancel",
            "candidate": {
                "name": candidate_name,
                "email": candidate_email,
                "interviewer": {
                    "name": interviewer_name,
                    "email": interviewer_email
                }
            }
        }
    return None

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
    job_profile = candidate.get('job_profile')
    if not job_profile or job_profile.lower() == "interview":
        job_profile = st.session_state.get("last_job_profile", "python developer")
    st.session_state["last_job_profile"] = job_profile

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "subject": f"Interview: {job_profile} with {candidate['name']}",
        "body": {
            "contentType": "HTML",
            "content": (
                f"Dear {candidate['name']},<br><br>"
                f"Your interview for <b>{job_profile}</b> with {interviewer['name']} has been scheduled."
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
    resp_json = r.json()
    st.session_state.scheduled_events[candidate['email']] = resp_json['id']
    # return resp_json['onlineMeeting']['joinUrl']    # <- Don't return joinUrl!
    return "success"

def cancel_teams_meeting(token, candidate_email):
    event_id = st.session_state.scheduled_events.get(candidate_email)
    if not event_id:
        raise ValueError(f"No scheduled meeting found for {candidate_email}")
    url = f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}/events/{event_id}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.delete(url, headers=headers)
    if r.status_code in [204, 200]:
        del st.session_state.scheduled_events[candidate_email]
        return True
    else:
        r.raise_for_status()
    return False

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

st.set_page_config(page_title="INTELLIBOT", layout="wide")
st.markdown(
    """
    <style>
    .app-header {
        font-size: 2.1rem !important;
        font-weight: 900 !important;
        text-align: center !important;
        color: #fff;
        background: linear-gradient(90deg, #6366f1, #10b981 70%);
        border-radius: 12px;
        margin-bottom: 8px;
        padding: 13px 0 11px 0;
        letter-spacing: 2px;
        box-shadow: 0 3px 12px #aaa2;
    }
    .chat-row {
        display: flex; width: 100%; margin-bottom: 0.32rem;
    }
    .user-msg {
        margin-left: auto;
        background: linear-gradient(90deg, #3b82f6 60%, #06b6d4 100%);
        color: #fff;
        border-radius: 16px 2px 16px 16px;
        padding: 9px 16px;
        max-width: 70%;
        box-shadow: 1px 2px 6px #ccc5;
        font-size: 0.98rem;
    }
    .bot-msg {
        margin-right: auto;
        background: linear-gradient(90deg, #f1f5f9, #e0e7ef 90%);
        color: #222;
        border-radius: 2px 16px 16px 16px;
        padding: 9px 16px;
        max-width: 80%;
        box-shadow: 1px 2px 6px #ccc3;
        font-size: 0.98rem;
    }
    .stDataFrame div[data-testid="stVerticalBlock"] {
        padding: 0 !important;
        margin: 0 !important;
    }
    .stDataFrame .css-1u3bzj6 {
        padding: 0 !important;
    }
    .stDataFrame th, .stDataFrame td {
        font-size: 13px !important;
        padding: 6px 8px !important;
        white-space: pre-line;
        word-break: break-word;
    }
    .stDataFrame table {
        width: 100% !important;
        min-width: 100% !important;
        border-collapse: collapse !important;
    }
    .stDataFrame tbody tr {
        border-bottom: 1px solid #eee;
    }
    .stDataFrame thead tr {
        background: #e5e9f3;
        border-bottom: 2px solid #6366f1;
    }
    .stDataFrame td {
        border-right: 1px solid #eee;
    }
    .stDataFrame th:last-child, .stDataFrame td:last-child {
        border-right: none;
    }
    </style>
    """,
    unsafe_allow_html=True
)
st.markdown('<div class="app-header">INTELLIBOT</div>', unsafe_allow_html=True)
st.write("I am Intellibot. How can I help you today for interview scheduling?")

if "history" not in st.session_state:
    st.session_state.history = []
if "candidate_table" not in st.session_state:
    st.session_state.candidate_table = pd.DataFrame()
if "chat_mode" not in st.session_state:
    st.session_state.chat_mode = True

agent  = project_client.agents.get_agent(AGENT_ID)
thread = project_client.agents.get_thread(THREAD_ID)

for msg in st.session_state.history:
    st.markdown(
        f'<div class="chat-row"><div class="user-msg">{msg["user"]}</div></div>',
        unsafe_allow_html=True
    )
    st.markdown(
        f'<div class="chat-row"><div class="bot-msg">{msg["bot"]}</div></div>',
        unsafe_allow_html=True
    )

if st.session_state.chat_mode:
    user_input = st.chat_input("Type your message and hit Enter…")
    if user_input:
        if user_input.strip().lower() == "exit":
            st.session_state.chat_mode = False
        else:
            st.markdown(
                f'<div class="chat-row"><div class="user-msg">{user_input}</div></div>',
                unsafe_allow_html=True
            )
            bot_reply = get_bot_reply(user_input, thread, agent)
            st.markdown(
                f'<div class="chat-row"><div class="bot-msg">{bot_reply}</div></div>',
                unsafe_allow_html=True
            )
            st.session_state.history.append({"user": user_input, "bot": bot_reply})

            sched_cancel_info = extract_schedule_cancel_info(bot_reply)
            if sched_cancel_info:
                try:
                    token = get_access_token()
                    if sched_cancel_info["action"] == "schedule":
                        c = sched_cancel_info["candidate"]
                        if CANDIDATE_EMAIL_OVERRIDE:
                            c['email'] = CANDIDATE_EMAIL_OVERRIDE
                        create_status = create_teams_meeting(token, c["interviewer"], c)
                        st.markdown(
                            f'<div class="chat-row"><div class="bot-msg">✅ Meeting scheduled successfully for {c["name"]} with {c["interviewer"]["name"]}.</div></div>',
                            unsafe_allow_html=True
                        )
                        st.session_state.history.append({"user": "", "bot": 'Meeting scheduled successfully.'})
                    elif sched_cancel_info["action"] == "cancel":
                        c = sched_cancel_info["candidate"]
                        cancelled = cancel_teams_meeting(token, c["email"])
                        if cancelled:
                            st.markdown(
                                f'<div class="chat-row"><div class="bot-msg">❌ Meeting cancelled for {c["name"]} ({c["email"]}).</div></div>',
                                unsafe_allow_html=True
                            )
                            st.session_state.history.append({"user": "", "bot": f'Meeting cancelled for {c["name"]} ({c["email"]})'})
                except Exception as e:
                    st.markdown(
                        f'<div class="chat-row"><div class="bot-msg">❌ Scheduling/Cancellation error: {e}</div></div>',
                        unsafe_allow_html=True
                    )

if st.session_state.candidate_table is not None and not st.session_state.candidate_table.empty:
    st.write("### All Candidate Details (including all key skills)")
    st.dataframe(st.session_state.candidate_table, use_container_width=True)

if not st.session_state.chat_mode:
    st.markdown(
        '<div class="chat-row"><div class="bot-msg"><b>Session complete. Reload the app to start new chat.</b></div></div>',
        unsafe_allow_html=True
    )
    chat_content = "\n".join([f"User: {e['user']}\nBot: {e['bot']}" for e in st.session_state.history])
    try:
        system = (
            "Extract all candidates and all their available key skills from the chat. "
            "For each candidate, show every skill present (do not skip any key skill). "
            "Return a list of candidate objects as JSON, with these columns: "
            "[Name, Email, Key Skill (comma-separated), Total Experience, Relevant Experience, Location, Notice Period, Interviewer Name, Interviewer Email, Date, Time, Job Profile]."
        )
        user = f"""Chat log:\n{chat_content}\nReturn the list as JSON array."""
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            "temperature": 0.1
        }
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        resp = requests.post(GROQ_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content']
        m = re.search(r'\[.*\]', content, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            df = pd.DataFrame(data)
            if 'Key Skill' in df.columns:
                df['Key Skill'] = df['Key Skill'].apply(lambda x: ', '.join(x) if isinstance(x, list) else x)
            st.session_state.candidate_table = df
            st.write("### All Candidate Details (including all key skills)")
            st.dataframe(df, use_container_width=True)
            if "Job Profile" in df.columns and not df["Job Profile"].isnull().all():
                job_profile = df["Job Profile"].dropna().astype(str).iloc[0]
                st.session_state["last_job_profile"] = job_profile
        else:
            st.warning("Candidate data could not be extracted. Try again.")
    except Exception as ex:
        st.error(f"Could not extract candidate table: {ex}")
