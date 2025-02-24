from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, Literal
import random
import time  # <-- For timing

from livekit import rtc
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    WorkerType,
    cli,
    llm,
)

from livekit.agents.multimodal import MultimodalAgent
from livekit.plugins import openai

from dotenv import load_dotenv
import os
import psycopg2

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DB_NAME = os.environ.get('DB_NAME')
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_HOST = os.environ.get('DB_HOST')
DB_PORT = os.environ.get('DB_PORT')

logger = logging.getLogger("my-worker")
logger.setLevel(logging.INFO)

# Constant for expected interview duration (in minutes)
EXPECTED_INTERVIEW_DURATION_MINUTES = 15

# -----------------------------------------------------------------------------
# Database Helper Functions
# -----------------------------------------------------------------------------
def get_questions(candidate_id):
    """
    Retrieve questions from the database for the given candidate.
    """
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    cursor = conn.cursor()
    query = """
    SELECT q.question_text
    FROM job_questions q
    JOIN candidates c ON q.job_title = c.job_title
    WHERE c.email = %s;
    """
    cursor.execute(query, (candidate_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows  # Each row is expected to be a tuple like (question_text,)

def save_report_to_db(participant_id: str, report_data: dict, interview_duration: int):
    """
    Save the interview report to the database.
    """
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
        )
        cursor = conn.cursor()
        query = """
        INSERT INTO interview_reports (participant_id, report_data, interview_duration)
        VALUES (%s, %s, %s)
        """
        cursor.execute(query, (participant_id, json.dumps(report_data), interview_duration))
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Report data saved to database.")
    except Exception as e:
        logger.error(f"Failed to save report: {e}")

def save_interview_state_to_db(participant_id: str, questions_asked: int, remaining_questions: list[str]):
    """
    Save the current interview state (number of questions asked and remaining questions)
    so that the interview can be resumed later.
    (Ensure that your DB has an 'interview_state' table with appropriate columns.)
    """
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
        )
        cursor = conn.cursor()
        query = """
        INSERT INTO interview_state (participant_id, questions_asked, remaining_questions)
        VALUES (%s, %s, %s)
        ON CONFLICT (participant_id) DO UPDATE
            SET questions_asked = EXCLUDED.questions_asked,
                remaining_questions = EXCLUDED.remaining_questions;
        """
        cursor.execute(query, (participant_id, questions_asked, json.dumps(remaining_questions)))
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Interview state saved to database.")
    except Exception as e:
        logger.error(f"Failed to save interview state: {e}")

report_structure = {
    "overallScore": "int",
    
    "performanceIndex": "string",
    "tableData": [
        {
            "parameter": "Clarity and Conciseness",
            "badge": "str",
            "badgeBgColor": "color",
            "badgeTextColor": "color",
            "assessment": "Did the user clearly explain the reason for the call (defaulted payment) and the next steps? Was the language easy to understand, avoiding jargon?",
            "observations": "str",
            "rating": "int/5"
        },
        {
            "parameter": "Compliance & Policy Understanding",
            "badge": "str",
            "badgeBgColor": "color",
            "badgeTextColor": "color",
            "assessment": "Evaluates whether the employee fully grasps and applies relevant policies, regulations, or standard operating guidelines",
            "observations": "str",
            "rating": "int/5"
        },
        {
            "parameter": "Completeness & Detail",
            "badge": "str",
            "badgeBgColor": "color",
            "badgeTextColor": "color",
            "assessment": "Checks if the employee's response includes all necessary details and covers every aspect of the question without omissions.",
            "observations": "str",
            "rating": "int/5"
        },
        {
            "parameter": "Communication Clarity",
            "badge": "str",
            "badgeBgColor": "color",
            "badgeTextColor": "color",
            "assessment": "Measures how clearly the employee conveys information—word choice, structure, and overall clarity of speech.",
            "observations": "str",
            "rating": "int/5"
        },
        {
            "parameter": "Resourcefulness & Initiative",
            "badge": "str",
            "badgeBgColor": "color",
            "badgeTextColor": "color",
            "assessment": "Do they know where to find the answer when they don't know it off the top of their head? How fast they find the answer, is it efficient?",
            "observations": "str",
            "rating": "int/5"
        },
        {
            "parameter": "Practical Application & Examples",
            "badge": "str",
            "badgeBgColor": "color",
            "badgeTextColor": "color",
            "assessment": "Looks at whether the employee uses real-world examples or scenarios to illustrate key points",
            "observations": "str",
            "rating": "int/5"
        },
        {
            "parameter": "Completeness",
            "badge": "str",
            "badgeBgColor": "color",
            "badgeTextColor": "color",
            "assessment": "Was answer satisfactory of missed points?",
            "observations": "str",
            "rating": "int/5"
        }
    ],
    "summary": "str",
    "transcription": "str",
    "evaluate": [
        {
            "Questions": "str",
            "areaToImprove": "str"
        },
        {
            "Questions": "str",
            "areaToImprove": "str"
        },
        {
            "Questions": "str",
            "areaToImprove": "str"
        },
        {
            "Questions": "str",
            "areaToImprove": "str"
        },
        {
            "Questions": "str",
            "areaToImprove": "str"
        }
    ]
}

# -----------------------------------------------------------------------------
# Instructions Template (includes step to record asked questions)
# -----------------------------------------------------------------------------
instructions_template = """You are Sanya, You are a Recruiter conducting a job interview.
              **Phase 1: Take Interview**
              **Instructions:**

            1.  **Begin:** Start the interview with "Hi, I am Sanya. Welcome to Cognet. Let's begin the Interview."

            2. **Question Selection:** You will draw from the provided question bank to ask **five** questions in random order, one question at a time. Do not present all the questions at once. Wait for a response from the interviewee before proceeding to the next question. Do not ask another question on the same topic.

            3. **Question Bank:** 
            {questions}

            4. **Question Presentation:** Present the question clearly and directly.

            5. **Response Handling:**
            - **If the interviewee provides a clear and accurate answer, acknowledge the correct response.**
            - **If the interviewee provides an unclear, partially correct, or incorrect answer, ask a relevant follow-up question to probe their understanding further.**
            - **If the interviewee states "I don't know" or otherwise indicates a lack of knowledge, acknowledge their response and move to the next question. Do not provide the answer.**

            6. **Follow-Up Questions:** Your follow-up questions should be designed to clarify the interviewee's answer.

            7. **Assessment:** After the fifth question (and its follow-up), conclude the session by saying: "Thank you! Your Interview is complete."

            8. **If the interviewee does not know the answer even after the follow-up, move to the next question.**

            9. **Do not answer questions yourself. Your role is to assess, not to teach during this interview.**
            
            10. Send report to database as soon as interview is complete.
            
             **Phase 2: JSON Generation**
            [CRITICAL: This phase must output ONLY valid JSON with no additional text]
            Be a strict interview and give score only if answer is relevent to the question otherwise give 0. If answer is partially completed give score between 1 and 4 based on answer.
            1. After saying the completion message, generate a valid JSON report that EXACTLY matches this schema:
            {report}

            2. JSON Requirements:
            * Rating scale: 1-5 integers only
            * Performance index: 0-100% string format
            * Badge values: Only "Excellent", "Good", or "Average"
            * Transcription format: "####Rosie: text\n####Executive: text\n\n"

            3. Badge Color Mapping:
            * Excellent (score > 4):
                "badgeBgColor": "green-100",
                "badgeTextColor": "green-600"
            * Good (score 3-4):
                "badgeBgColor": "blue-100",
                "badgeTextColor": "blue-900"
            * Average (score < 3):
                "badgeBgColor": "orange-100",
                "badgeTextColor": "orange-600"

            4. Before output:
            * Validate JSON structure
            * Verify all required fields are present
            * Ensure no extra fields or text
            * Check for proper nesting and formatting
            * Verify all string values are properly quoted
            * Confirm all arrays are properly terminated
            * Ensure proper syntax. If data is missing, return an appropriate error message.
            
            [CRITICAL: Final output must be ONLY the JSON object. No markdown, no explanations, no additional text]

            ERROR PREVENTION:
            * Do not include any text outside the JSON structure
            * Do not add commentary or explanations
            * Do not use markdown formatting or code blocks
            * Do not include disclaimers or notes
            * If JSON validation fails, regenerate until valid
            * Never mix conversation text with JSON output

            After JSON generation, respond only with "Thanks" and end the session.
            Do not make any mistake while creating report, otherwise it will be considerd as not obeying rules.
"""

# -----------------------------------------------------------------------------
# Session Configuration
# -----------------------------------------------------------------------------
@dataclass
class SessionConfig:
    instructions: str
    voice: openai.realtime.api_proto.Voice
    temperature: float
    max_response_output_tokens: str | int
    modalities: list[openai.realtime.api_proto.Modality]
    turn_detection: openai.realtime.ServerVadOptions

    def __post_init__(self):
        if self.modalities is None:
            self.modalities = self._modalities_from_string("text_and_audio")

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if k != "OPENAI_API_KEY"}

    @staticmethod
    def _modalities_from_string(modalities: str) -> list[str]:
        modalities_map = {
            "text_and_audio": ["text", "audio"],
            "text_only": ["text"],
        }
        return modalities_map.get(modalities, ["text", "audio"])

    def __eq__(self, other: SessionConfig) -> bool:
        return self.to_dict() == other.to_dict()

def parse_session_config(data: Dict[str, Any]) -> SessionConfig:
    if data.get("turn_detection"):
        turn_detection_json = json.loads(data.get("turn_detection"))
        turn_detection = openai.realtime.ServerVadOptions(
            threshold=turn_detection_json.get("threshold", 0.5),
            prefix_padding_ms=turn_detection_json.get("prefix_padding_ms", 200),
            silence_duration_ms=turn_detection_json.get(5000, 5000),
        )
    else:
        turn_detection = openai.realtime.DEFAULT_SERVER_VAD_OPTIONS

    config = SessionConfig(
        instructions="",  # To be set later
        voice=data.get("voice", "coral"),
        temperature=float(data.get("temperature", 0.4)),
        max_response_output_tokens=(data.get("max_output_tokens")
                                    if data.get("max_output_tokens") == "inf"
                                    else int(data.get("max_output_tokens") or 4000)),
        modalities=SessionConfig._modalities_from_string(data.get("modalities", "text_and_audio")),
        turn_detection=turn_detection,
    )
    return config

# -----------------------------------------------------------------------------
# Timeout and Call Ending
# -----------------------------------------------------------------------------
async def end_call(ctx: JobContext, reason: str):
    logger.info(f"Ending call: {reason}")
    await ctx.room.disconnect()

async def monitor_timeout(ctx: JobContext):
    try:
        await asyncio.sleep(15 * 60)  # 15 minutes timeout
        await end_call(ctx, "15-minute timeout reached")
    except asyncio.CancelledError:
        logger.info("Timeout cancelled: Report data sent before 15 minutes.")

# -----------------------------------------------------------------------------
# Main Entrypoint and Agent Logic
# -----------------------------------------------------------------------------
async def entrypoint(ctx: JobContext):
    logger.info(f"Connecting to room {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    participant = await ctx.wait_for_participant()
    timeout_task = asyncio.create_task(monitor_timeout(ctx))
    run_multimodal_agent(ctx, participant, timeout_task)
    logger.info("Agent started")

def run_multimodal_agent(ctx: JobContext, participant: rtc.Participant, timeout_task: asyncio.Task):
    # Parse session metadata
    metadata = json.loads(participant.metadata)
    logger.info(f"Metadata for participant: {metadata}")
    config = parse_session_config(metadata)
    user_id = metadata.get("user_id") or participant.identity
    logger.info(f"User ID: {user_id}")
    start_time = time.time()

    # -----------------------------------------------------------------------------
    # Set up state for tracking questions
    # -----------------------------------------------------------------------------
    question_bank = get_questions(user_id)
    logger.info("interview questions: ", question_bank)
    if not question_bank:
        logger.error("No questions retrieved from the database.")
        questions_formatted = "No questions available."
        sampled_questions = []
    else:
        # Check if there is saved state in the metadata (for a resumed interview)
        if metadata.get("remaining_questions"):
            try:
                remaining_questions = json.loads(metadata["remaining_questions"])
            except Exception as e:
                logger.error(f"Error parsing remaining_questions: {e}")
                remaining_questions = [q[0] for q in random.sample(question_bank, 5)]
        else:
            sampled_questions = random.sample(question_bank, 5)
            remaining_questions = [q[0] for q in sampled_questions]
        questions_formatted = "\n".join(f"- {q}" for q in remaining_questions)

    # Initialize counters for state tracking
    asked_count = 0

    # Update instructions with the current question bank state
    config.instructions = instructions_template.format(questions=questions_formatted, report=report_structure)
    logger.info("instructions: ", config.instructions)

    if not OPENAI_API_KEY:
        raise Exception("OpenAI API Key is required")

    model = openai.realtime.RealtimeModel(
        api_key=OPENAI_API_KEY,
        instructions=config.instructions,
        voice='coral',
        temperature=0.7,
        max_response_output_tokens=config.max_response_output_tokens,
        modalities=config.modalities,
        turn_detection=config.turn_detection,
    )

    fnc_ctx = llm.FunctionContext()

    @fnc_ctx.ai_callable(
        name="send_report_data",
        description="Send interview report data to the database based on the conversation as soon as agent says thanks."
    )
    async def send_report_data(evaluation_report: str):
        try:
            logger.info(f"Received report data from {participant.identity}")
            report_data = json.loads(evaluation_report)
            seconds = time.time() - start_time
            actual_seconds = round(seconds)
            
            report_data["participant_id"] = user_id
            report_data["interview_duration"] = actual_seconds
            logger.info(f"Report data with timing: {report_data}")
            save_report_to_db(user_id, report_data, actual_seconds)
            if not timeout_task.done():
                timeout_task.cancel()
            await end_call(ctx, "Report data sent to database")
        except Exception as e:
            logger.error(f"Error in send_report_data: {e}")

    @fnc_ctx.ai_callable(
        name="record_question_asked",
        description="Record that a question was asked by the agent."
    )
    async def record_question_asked(question_text: str):
        nonlocal asked_count, remaining_questions
        asked_count += 1
        if question_text in remaining_questions:
            remaining_questions.remove(question_text)
        logger.info(f"Recorded question: '{question_text}'. Total asked: {asked_count}")

    assistant = MultimodalAgent(model=model, fnc_ctx=fnc_ctx)

    def save_transcription(text, filename):
        with open(filename, 'w') as f:
            f.write(text)

    def save_audio(audio_data, filename):
        with open(filename, 'wb') as f:
            f.write(audio_data)

    @assistant.on("transcription_received")
    def on_transcription_received(transcription):
        logger.info(f"Transcription received: {transcription}")
        save_transcription(transcription, "transcription.txt")

    @assistant.on("audio_received")
    def on_audio_received(audio_data):
        logger.info("Audio received")
        save_audio(audio_data, "audio.wav")

    assistant.start(ctx.room)
    session = model.sessions[0]

    if config.modalities == ["text", "audio"]:
        session.conversation.item.create(
            llm.ChatMessage(
                role="user",
                content="You are Sanya. ",
            )
        )
        session.response.create()

    @ctx.room.local_participant.register_rpc_method("pg.updateConfig")
    async def update_config(data: rtc.rpc.RpcInvocationData):
        if data.caller_identity != participant.identity:
            return

        new_config = parse_session_config(json.loads(data.payload))
        if config != new_config:
            logger.info(f"Config changed: {new_config.to_dict()}, participant: {participant.identity}")
            session = model.sessions[0]
            session.session_update(
                instructions=new_config.instructions,
                voice=new_config.voice,
                temperature=new_config.temperature,
                max_response_output_tokens=new_config.max_response_output_tokens,
                turn_detection=new_config.turn_detection,
                modalities=new_config.modalities,
            )
            return json.dumps({"changed": True})
        else:
            return json.dumps({"changed": False})

    @session.on("response_done")
    def on_response_done(response: openai.realtime.RealtimeResponse):
        variant: Literal["warning", "destructive"]
        description: str | None = None
        title: str
        if response.status == "incomplete":
            if response.status_details and response.status_details.get("reason"):
                reason = response.status_details["reason"]
                if reason == "max_output_tokens":
                    variant = "warning"
                    title = "Max output tokens reached"
                    description = "Response may be incomplete"
                elif reason == "content_filter":
                    variant = "warning"
                    title = "Content filter applied"
                    description = "Response may be incomplete"
                else:
                    variant = "warning"
                    title = "Response incomplete"
            else:
                variant = "warning"
                title = "Response incomplete"
        elif response.status == "failed":
            if response.status_details and response.status_details.get("error"):
                error_code = response.status_details["error"]["code"]
                if error_code == "server_error":
                    variant = "destructive"
                    title = "Server error"
                elif error_code == "rate_limit_exceeded":
                    variant = "destructive"
                    title = "Rate limit exceeded"
                else:
                    variant = "destructive"
                    title = "Response failed"
            else:
                variant = "destructive"
                title = "Response failed"
        else:
            return

        asyncio.create_task(show_toast(title, description, variant))

    async def send_transcription(ctx: JobContext, participant: rtc.Participant, track_sid: str,
                                 segment_id: str, text: str, is_final: bool = True):
        transcription = rtc.Transcription(
            participant_identity=participant.identity,
            track_sid=track_sid,
            segments=[
                rtc.TranscriptionSegment(
                    id=segment_id,
                    text=text,
                    start_time=0,
                    end_time=0,
                    language="en",
                    final=is_final,
                )
            ],
        )
        await ctx.room.local_participant.publish_transcription(transcription)

    async def show_toast(title: str, description: str | None,
                         variant: Literal["default", "success", "warning", "destructive"]):
        await ctx.room.local_participant.perform_rpc(
            destination_identity=participant.identity,
            method="pg.toast",
            payload=json.dumps({"title": title, "description": description, "variant": variant}),
        )

    last_transcript_id = None

    @session.on("input_speech_started")
    def on_input_speech_started():
        nonlocal last_transcript_id
        remote_participant = next(iter(ctx.room.remote_participants.values()), None)
        if not remote_participant:
            return
        track_sid = next(
            (track.sid for track in remote_participant.track_publications.values()
             if track.source == rtc.TrackSource.SOURCE_MICROPHONE), None)
        if last_transcript_id:
            asyncio.create_task(send_transcription(ctx, remote_participant, track_sid, last_transcript_id, ""))
        new_id = str(uuid.uuid4())
        last_transcript_id = new_id
        asyncio.create_task(send_transcription(ctx, remote_participant, track_sid, new_id, "…", is_final=False))

    @session.on("input_speech_transcription_completed")
    def on_input_speech_transcription_completed(event: openai.realtime.InputTranscriptionCompleted):
        nonlocal last_transcript_id
        if last_transcript_id:
            remote_participant = next(iter(ctx.room.remote_participants.values()), None)
            if not remote_participant:
                return
            track_sid = next(
                (track.sid for track in remote_participant.track_publications.values()
                 if track.source == rtc.TrackSource.SOURCE_MICROPHONE), None)
            asyncio.create_task(send_transcription(ctx, remote_participant, track_sid, last_transcript_id, ""))
            last_transcript_id = None

    @session.on("input_speech_transcription_failed")
    def on_input_speech_transcription_failed(event: openai.realtime.InputTranscriptionFailed):
        nonlocal last_transcript_id
        if last_transcript_id:
            remote_participant = next(iter(ctx.room.remote_participants.values()), None)
            if not remote_participant:
                return
            track_sid = next(
                (track.sid for track in remote_participant.track_publications.values()
                 if track.source == rtc.TrackSource.SOURCE_MICROPHONE), None)
            error_message = "⚠️ Transcription failed"
            asyncio.create_task(send_transcription(ctx, remote_participant, track_sid, last_transcript_id, error_message))
            last_transcript_id = None

    # -----------------------------------------------------------------------------
    # Listen for participant disconnection to save interview state
    # -----------------------------------------------------------------------------
    @ctx.room.on("participant_disconnected")
    async def on_participant_disconnected(disconnected_participant: rtc.Participant):
        if disconnected_participant.identity == participant.identity:
            logger.info("Participant disconnected. Saving interview state.")
            save_interview_state_to_db(user_id, asked_count, remaining_questions)
            await end_call(ctx, "Participant disconnected")

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, worker_type=WorkerType.ROOM))
