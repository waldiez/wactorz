# SDK 1.12 hardware smoke test

Status: **not performed by the security remediation**. Passing automated tests permits
experimental `dev` review; production hardware support requires the owner to complete
this checklist on the platforms being supported.

Record the Wactorz commit, SDK version, operating system, Python version, Wireless/Lite
model, daemon version and connection/media backend. Use a clear table and keep an
operator at the controls. Check that `health` reports the expected configuration.

- [ ] **Connection:** start Wactorz, spawn `reachy-mini`, connect to the intended robot,
      and confirm initialization completes without repeated errors or unintended motion.
- [ ] **Movement:** test wake, look left/right, antenna motion, a gesture, and stop.
      Confirm controlled movement and that stop interrupts it.
- [ ] **Speech:** say a short phrase and a longer reply; check playback, volume and stop.
- [ ] **Microphone:** test push-to-talk and a short conversation. Check transcription,
      reply playback, stop-listening behavior and absence of microphone feedback loops.
      Confirm an administrative request is refused by the robot bridge; perform any
      actual administrative work only through the existing operator interface.
- [ ] **Camera/media:** capture a frame and, where supported, ask what Reachy sees.
      Check media recovery and an actionable refusal where the backend lacks media.
- [ ] **Recovery:** disconnect or power off the robot during an ordinary request,
      reconnect it, and confirm bounded failure messages and recovery without duplicate
      sessions, stuck speech, or continued motion after stop.
- [ ] **Restart:** restart the agent, then Wactorz. Confirm connection, saved settings,
      movement, speech and microphone behavior remain normal afterward.

Record pass/fail/not-supported for each item, with relevant logs and recovery steps.
An untested platform or missing media backend must remain documented as such. A failure
affecting claimed support blocks that hardware release; do not infer a pass from an
SDK import, fake-device test or CI result.
