from __future__ import annotations

from pathlib import Path
from typing import Any

import attr

from mentat.errors import HistoryError, MentatError
from mentat.parsers.change_display_helper import (
    DisplayInformation,
    FileActionType,
    change_delimiter,
    get_full_change,
)
from mentat.session_context import SESSION_CONTEXT
from mentat.session_input import ask_yes_no
from mentat.utils import get_relative_path


# TODO: Add 'owner' to Replacement so that interactive mode can accept/reject multiple replacements at once
@attr.define(order=False)
class Replacement:
    """
    Represents that the lines from starting_line (inclusive) to ending_line (exclusive)
    should be replaced with new_lines
    """

    # Inclusive
    starting_line: int = attr.field()
    # Exclusive
    ending_line: int = attr.field()

    new_lines: list[str] = attr.field()

    def __lt__(self, other: Replacement):
        return self.ending_line < other.ending_line or (
            self.ending_line == other.ending_line
            and self.starting_line < other.ending_line
        )


async def _ask_user_change(
    text: str,
) -> bool:
    session_context = SESSION_CONTEXT.get()
    stream = session_context.stream

    stream.send(text, color="light_blue")
    return await ask_yes_no(default_yes=True)


@attr.define
class FileEdit:
    """
    Represents that this file_path content should have specified Replacements applied to it.
    Can also represent that this file should be created, deleted, or is being renamed.
    """

    # Should be abs path
    file_path: Path = attr.field()
    replacements: list[Replacement] = attr.field(factory=list)
    is_creation: bool = attr.field(default=False)
    is_deletion: bool = attr.field(default=False)
    # Should be abs path
    rename_file_path: Path | None = attr.field(default=None)

    # Used for undo
    previous_file_lines: list[str] | None = attr.field(default=None)

    @file_path.validator  # pyright: ignore
    def is_abs_path(self, attribute: attr.Attribute[Path], value: Any):
        if not isinstance(value, Path):
            raise ValueError(f"File_path must be a Path, got {type(value)}")
        if not value.is_absolute():
            raise ValueError(f"File_path must be an absolute path, got {value}")

    def _display_creation(self, prefix: str = ""):
        ctx = SESSION_CONTEXT.get()

        added_lines = list[str]()
        for replacement in self.replacements:
            added_lines.extend(replacement.new_lines)
        display_information = DisplayInformation(
            self.file_path, [], added_lines, [], FileActionType.CreateFile
        )
        ctx.stream.send(get_full_change(display_information, prefix=prefix))

    def _display_deletion(self, file_lines: list[str], prefix: str = ""):
        ctx = SESSION_CONTEXT.get()

        display_information = DisplayInformation(
            self.file_path,
            [],
            [],
            file_lines,
            FileActionType.DeleteFile,
        )
        ctx.stream.send(get_full_change(display_information, prefix=prefix))

    def _display_rename(self, prefix: str = ""):
        ctx = SESSION_CONTEXT.get()

        display_information = DisplayInformation(
            self.file_path,
            [],
            [],
            [],
            FileActionType.RenameFile,
            new_name=self.rename_file_path,
        )
        ctx.stream.send(get_full_change(display_information, prefix=prefix))

    def _display_replacement(
        self, replacement: Replacement, file_lines: list[str], prefix: str = ""
    ):
        ctx = SESSION_CONTEXT.get()

        removed_block = file_lines[replacement.starting_line : replacement.ending_line]
        display_information = DisplayInformation(
            self.file_path,
            file_lines,
            replacement.new_lines,
            removed_block,
            FileActionType.UpdateFile,
            replacement.starting_line,
            replacement.ending_line,
            self.rename_file_path,
        )
        ctx.stream.send(get_full_change(display_information, prefix=prefix))

    def _display_replacements(self, file_lines: list[str], prefix: str = ""):
        for replacement in self.replacements:
            self._display_replacement(replacement, file_lines, prefix=prefix)

    def display_full_edit(self, file_lines: list[str], prefix: str = ""):
        """Displays the full edit as if it were altering a file with the lines given"""
        if self.is_deletion:
            self._display_deletion(file_lines, prefix=prefix)
        if self.rename_file_path:
            self._display_rename(prefix=prefix)
        if self.is_creation:
            self._display_creation(prefix=prefix)
        else:
            self._display_replacements(file_lines, prefix=prefix)

    def is_valid(self) -> bool:
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream
        code_context = session_context.code_context

        display_path = get_relative_path(self.file_path, session_context.cwd)

        if self.is_creation:
            if self.file_path.exists():
                stream.send(
                    f"File {display_path} already exists, canceling creation.",
                    color="light_yellow",
                )
                return False
        else:
            if not self.file_path.exists():
                stream.send(
                    f"File {display_path} does not exist, canceling all edits to file.",
                    color="light_yellow",
                )
                return False
            file_features_in_context = [
                f for f in code_context.features if f.path == self.file_path
            ] or code_context.include_files.get(self.file_path, [])
            if not all(
                any(f.contains_line(i) for f in file_features_in_context)
                for r in self.replacements
                for i in range(r.starting_line, r.ending_line)
            ):
                stream.send(
                    f"File {display_path} not in context, canceling all edits to file.",
                    color="light_yellow",
                )
                return False

        if self.rename_file_path is not None and self.rename_file_path.exists():
            rel_rename_path = None
            if self.rename_file_path.is_relative_to(session_context.cwd):
                rel_rename_path = self.rename_file_path.relative_to(session_context.cwd)
            stream.send(
                f"File {display_path} being renamed to existing file"
                f" {rel_rename_path or self.rename_file_path}, canceling rename.",
                color="light_yellow",
            )
            self.rename_file_path = None
        return True

    async def filter_replacements(
        self,
    ) -> bool:
        session_context = SESSION_CONTEXT.get()
        code_file_manager = session_context.code_file_manager

        if self.is_creation:
            self._display_creation()
            if not await _ask_user_change("Create this file?"):
                return False
            file_lines = []
        else:
            file_lines = code_file_manager.file_lines[self.file_path]

        if self.is_deletion:
            self._display_deletion(file_lines)
            if not await _ask_user_change("Delete this file?"):
                return False

        if self.rename_file_path is not None:
            self._display_rename()
            if not await _ask_user_change("Rename this file?"):
                self.rename_file_path = None

        if not self.is_creation:
            new_replacements = list[Replacement]()
            for replacement in self.replacements:
                self._display_replacement(replacement, file_lines)
                if await _ask_user_change("Keep this change?"):
                    new_replacements.append(replacement)
            self.replacements = new_replacements

        return (
            self.is_creation
            or self.is_deletion
            or (self.rename_file_path is not None)
            or len(self.replacements) > 0
        )

    def _print_resolution(self, first: Replacement, second: Replacement):
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream

        stream.send("Change overlap detected, auto-merged back to back changes:\n")
        stream.send(self.file_path)
        stream.send(change_delimiter)
        for line in first.new_lines + second.new_lines:
            stream.send("+ " + line, color="green")
        stream.send(change_delimiter)

    def resolve_conflicts(self):
        self.replacements.sort(reverse=True)
        for index, replacement in enumerate(self.replacements):
            for other in self.replacements[index + 1 :]:
                if (
                    other.ending_line > replacement.starting_line
                    and other.starting_line < replacement.ending_line
                ):
                    # Overlap conflict
                    other.ending_line = replacement.starting_line
                    other.starting_line = min(other.starting_line, other.ending_line)
                    self._print_resolution(other, replacement)
                elif (
                    other.ending_line == other.starting_line
                    and replacement.ending_line == replacement.starting_line
                    and replacement.starting_line == other.starting_line
                ):
                    # Insertion conflict
                    # This will be a bit wonky if there are more than 2 insertion conflicts on the same line
                    self._print_resolution(replacement, other)

    def get_updated_file_lines(self, file_lines: list[str]):
        self.replacements.sort(reverse=True)
        earliest_line = None
        for replacement in self.replacements:
            if earliest_line is not None and replacement.ending_line > earliest_line:
                # This should never happen if resolve conflicts is called
                raise MentatError("Error: Line overlap in Replacements")
            if replacement.ending_line > len(file_lines):
                file_lines += [""] * (replacement.ending_line - len(file_lines))
            earliest_line = replacement.starting_line
            file_lines = (
                file_lines[: replacement.starting_line]
                + replacement.new_lines
                + file_lines[replacement.ending_line :]
            )
        return file_lines

    def undo(self):
        ctx = SESSION_CONTEXT.get()

        prefix = "UNDO: "

        if self.is_creation:
            if not self.file_path.exists():
                raise HistoryError(
                    f"File {self.file_path} does not exist; unable to delete"
                )
            ctx.code_file_manager.delete_file(self.file_path)

            self._display_creation(prefix=prefix)
            ctx.stream.send(
                f"Creation of file {self.file_path} undone", color="light_blue"
            )
            return

        if self.rename_file_path is not None:
            if self.file_path.exists():
                raise HistoryError(
                    f"File {self.file_path} already exists; unable to undo rename to"
                    f" {self.rename_file_path}"
                )
            if not self.rename_file_path.exists():
                raise HistoryError(
                    f"File {self.rename_file_path} does not exist; unable to undo"
                    f" rename from {self.file_path}"
                )
            ctx.code_file_manager.rename_file(self.rename_file_path, self.file_path)

            self._display_rename(prefix=prefix)
            ctx.stream.send(
                f"Rename of file {self.file_path} to {self.rename_file_path} undone",
                color="light_blue",
            )

        if self.is_deletion:
            if self.file_path.exists():
                raise HistoryError(
                    f"File {self.file_path} already exists; unable to re-create"
                )
            if not self.previous_file_lines:
                # Should never happen
                raise ValueError(
                    "Previous file lines not set when undoing file deletion"
                )
            ctx.code_file_manager.create_file(
                self.file_path, content="\n".join(self.previous_file_lines)
            )

            self._display_deletion(self.previous_file_lines, prefix=prefix)
            ctx.stream.send(
                f"Deletion of file {self.file_path} undone", color="light_red"
            )
        elif self.replacements:
            if not self.file_path.exists():
                raise HistoryError(
                    f"File {self.file_path} does not exist; unable to undo edit"
                )
            if not self.previous_file_lines:
                # Should never happen
                raise ValueError("Previous file lines not set when undoing file edit")

            with open(self.file_path, "w") as f:
                f.write("\n".join(self.previous_file_lines))

            self._display_replacements(self.previous_file_lines, prefix=prefix)
            ctx.stream.send(
                f"Edits to file {self.file_path} undone", color="light_blue"
            )
