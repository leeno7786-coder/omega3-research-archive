// Panel: help — static operator guide (no live telemetry).
// Registry: PANELS.help.body -> panelHelp
function panelHelp(b) {
  const E = _escapeHtml;
  const root = _ensurePanelRoot(b, 'help');
  root.innerHTML = `
        <div class="help-content">
          <div class="panel-label">about omega</div>
          <div class="line">
            <strong>Omega</strong> is a portable cognitive substrate — a local, self-contained AI assistant 
            that operates as a pressure-driven cognition loop. It processes information through 
            an autonomic cycle, building memory and identity over time.
          </div>
          <div class="line">&nbsp;</div>
          <div class="panel-label section">navigation</div>
          <div class="line bullet">Open the left <code>System controls</code> rail for <code>settings</code>, <code>help</code>, and <code>shutdown</code>.</div>
          <div class="line bullet">Use <code>Esc</code> to close any open panel</div>
          <div class="line bullet">Voice commands: say "omega, open settings" when voice input is enabled</div>
          <div class="line">&nbsp;</div>
          <div class="panel-label section">what to expect</div>
          <div class="line bullet"><strong>settings</strong> — operator profile, themes, voice, memory graph, and plugins</div>
          <div class="line">&nbsp;</div>
          <div class="panel-label section">system messages</div>
          <div class="line bullet"><strong>ASK_OPERATOR</strong> questions now appear in the main chat feed (and are spoken aloud) AND here in the message box, where you answer them so the system knows they're handled</div>
          <div class="line">
            When Omega forms a question for you, it appears as a chat message with a clickable note —
            click it (or the <strong>bubble badge</strong> beside the composer) to open this box and reply.
            Your answer is recorded back to the proactive system.
          </div>
          <div class="line">&nbsp;</div>
          <div class="panel-label section">how it works</div>
          <div class="line bullet">Omega reflects → forms a question → it competes for attention like every other internal signal</div>
          <div class="line bullet">If it wins, Omega writes you a message → the badge lights up → open the panel to read it</div>
          <div class="line bullet"><strong>respond</strong> sends your answer back into Omega's mind — it closes the question and uses your answer as signal</div>
          <div class="line bullet"><strong>delete</strong> dismisses it — Omega is unaffected and may ask again later if it still matters</div>
          <div class="line bullet">Replies are never lost — they queue in order and each gets its own response</div>
          <div class="line">&nbsp;</div>
          <div class="panel-label section">tips</div>
          <div class="line bullet">Complete your operator profile in <code>settings → operator profile</code> so Omega can recognize you</div>
          <div class="line bullet">Add face memory to help Omega match names to faces</div>
          <div class="line bullet">Use the chat interface to ask questions and give commands</div>
          <div class="line bullet">The system learns and adapts based on your interactions over time</div>
        </div>
      `;
}
