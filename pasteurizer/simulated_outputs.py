"""Record output commands without accessing hardware."""

from dataclasses import asdict

from pasteurizer.output_commands import OutputCommands


class SimulatedOutputWriter:
    def __init__(self):
        # None means no command set has been written yet.
        self.last_commands: OutputCommands | None = None
        self.write_count = 0

    def write(self, commands: OutputCommands):
        """Accept a complete command set and display changes."""
        if not isinstance(commands, OutputCommands):
            raise TypeError("Expected an OutputCommands instance")

        previous = (
            asdict(self.last_commands)
            if self.last_commands is not None
            else {}
        )

        for name, value in asdict(commands).items():
            if name not in previous or previous[name] != value:
                print(f"[SIMULATED OUTPUT] {name} = {value}")

        self.last_commands = commands
        self.write_count += 1