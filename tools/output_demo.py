"""Check simulated output writing without accessing hardware."""

from pasteurizer.output_commands import OutputCommands
from pasteurizer.simulated_outputs import SimulatedOutputWriter


writer = SimulatedOutputWriter()

print("\n1. Establish initial commands")
writer.write(OutputCommands())

print("\n2. Request transfer pump 1 ON")
writer.write(OutputCommands(transfer_pump_1=True))
assert writer.last_commands.transfer_pump_1 is True

print("\n3. Repeat the same request — no output changes expected")
writer.write(OutputCommands(transfer_pump_1=True))

print("\n4. Request transfer pump 1 OFF")
writer.write(OutputCommands())
assert writer.last_commands.transfer_pump_1 is False

assert writer.write_count == 4

print("\nAll simulated output checks passed.")