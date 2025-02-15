import asyncio
import logging
import queue
from asyncio import Event
from contextlib import asynccontextmanager
from timeit import default_timer
from typing import Any

import numpy as np

try:
    import sounddevice as sd
    import soundfile as sf

    audio_available = True
except Exception:
    audio_available = False


from mentat.command.command import Command
from mentat.logging_config import logs_path
from mentat.session_context import SESSION_CONTEXT

RATE = 16000


class Recorder:
    def __init__(self):
        self.shutdown = Event()
        self._interrupt_task = None
        (logs_path / "audio").mkdir(parents=True, exist_ok=True)

        self.file = logs_path / "audio/talk_transcription.wav"

    async def listen_for_interrupt(self):
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream

        async with stream.interrupt_lock:
            await stream.recv("interrupt")
            logging.info("User interrupted response.")
            self.shutdown.set()

    def callback(
        self,
        in_data: np.ndarray[Any, Any],
        frames: int,
        time: Any,
        status: int,
    ):
        self.q.put(in_data.copy())

    async def record(self):
        self.start_time = default_timer()

        self.q: queue.Queue[np.ndarray[Any, Any]] = queue.Queue()
        with sf.SoundFile(self.file, mode="w", samplerate=RATE, channels=1) as file:
            with sd.InputStream(samplerate=RATE, channels=1, callback=self.callback):
                while not self.shutdown.is_set():
                    await asyncio.sleep(0)
                    file.write(self.q.get())  # type: ignore

        self.recording_time = default_timer() - self.start_time

    @asynccontextmanager
    async def interrupt_catcher(self):
        self._interrupt_task = asyncio.create_task(self.listen_for_interrupt())
        yield
        if self._interrupt_task is not None:  # type: ignore
            self._interrupt_task.cancel()
            try:
                await self._interrupt_task
            except asyncio.CancelledError:
                pass
        self._interrupt_task = None
        self.shutdown.clear()


class TalkCommand(Command, command_name="talk"):
    async def apply(self, *args: str) -> None:
        ctx = SESSION_CONTEXT.get()
        if not audio_available:
            # sounddevice manages port audio on Mac and Windows so we print an apt specific message
            ctx.stream.send(
                "Audio is not available on this system. You probably need to install"
                " PortAudio. For example `sudo apt install libportaudio2` on Ubuntu.",
                color="light_red",
            )
        else:
            ctx.stream.send(
                "Listening on your default microphone. Press Ctrl+C to end."
            )
            recorder = Recorder()
            async with recorder.interrupt_catcher():
                await recorder.record()
            ctx.stream.send("Processing audio with whisper...")
            await asyncio.sleep(0.01)
            transcript = await ctx.llm_api_handler.call_whisper_api(recorder.file)
            ctx.stream.send(transcript, channel="default_prompt")
            ctx.cost_tracker.log_whisper_call_stats(recorder.recording_time)

    @classmethod
    def argument_names(cls) -> list[str]:
        return ["command"]

    @classmethod
    def help_message(cls) -> str:
        return "Start voice to text."
