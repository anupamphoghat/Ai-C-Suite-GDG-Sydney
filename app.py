import streamlit as st
import asyncio
import os
from dotenv import load_dotenv
from google.cloud import storage
from google.antigravity import Agent, LocalAgentConfig, types

load_dotenv()

# --- Config ---
BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
if not BUCKET_NAME:
    st.error("GCS_BUCKET_NAME is not set. Please check .env or deployment configuration.")
SAVE_DIR = "/tmp/antigravity_sessions"
os.makedirs(SAVE_DIR, exist_ok=True)

# --- Load Master Context ---
def load_master_context():
    context = ""
    try:
        with open("00_Master_Context/Business_Context.md", "r") as f:
            context += f.read() + "\n\n"
        with open("00_Master_Context/Brand_Voice.md", "r") as f:
            context += f.read() + "\n\n"
    except Exception as e:
        context = f"Error loading context: {e}"
    return context

# --- GCS Upload ---
def upload_to_gcs(file_obj, destination_blob_name):
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(destination_blob_name)
        blob.upload_from_file(file_obj)
        return f"gs://{BUCKET_NAME}/{destination_blob_name}"
    except Exception as e:
        st.error(f"Failed to upload to GCS: {e}")
        return None

# --- Agent Chat ---
async def chat_with_agent(prompt: str, conversation_id: str = None):
    system_instructions_text = (
        "You are the CEO Proxy Agent for GlobalTech Solutions. You receive instructions from the CEO and delegate tasks to your subagents (CFO, CHRO, CMO, CSO, CTO) based on their skills. "
        "Use the master context below to ensure all decisions align with the business context and brand voice.\n\n"
        f"### Master Context:\n{load_master_context()}"
    )
    
    config = LocalAgentConfig(
        system_instructions=system_instructions_text,
        capabilities=types.CapabilitiesConfig(enable_subagents=True),
        skills_paths=["01_Executive_Agents"],
        save_dir=SAVE_DIR,
        conversation_id=conversation_id
    )
    
    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        text = await response.text()
        return text, agent.conversation_id

# --- Streamlit UI ---
st.set_page_config(page_title="CEO Dashboard - Multi-Agent System", layout="wide")
st.title("CEO Dashboard")
st.markdown("Give instructions to the AI executive team.")

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None
if "messages" not in st.session_state:
    st.session_state.messages = []

# File Upload Section (Main Dashboard)
with st.expander("Upload File for Analysis", expanded=False):
    uploaded_file = st.file_uploader("Upload a document for the C-Suite agents to analyze")
    if st.button("Upload to GCS") and uploaded_file is not None:
        with st.spinner("Uploading..."):
            gcs_uri = upload_to_gcs(uploaded_file, uploaded_file.name)
            if gcs_uri:
                st.success(f"Uploaded to {gcs_uri}")
                st.session_state.messages.append({"role": "system", "content": f"User uploaded file to GCS: {gcs_uri}. Tell the agent to refer to this URI if needed for analysis."})

# Chat Interface
for message in st.session_state.messages:
    if message["role"] != "system":
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    else:
        st.info(message["content"])

if prompt := st.chat_input("Enter your instruction..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
        
    with st.chat_message("assistant"):
        with st.spinner("Agents are thinking..."):
            try:
                response_text, new_conv_id = asyncio.run(chat_with_agent(prompt, st.session_state.conversation_id))
                st.session_state.conversation_id = new_conv_id
                st.markdown(response_text)
                st.session_state.messages.append({"role": "assistant", "content": response_text})
            except Exception as e:
                st.error(f"Error during agent execution: {e}")
