import React from "react";
import {
  AbsoluteFill,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const BG = "#0b0f14";
const PANEL = "#11161d";
const BORDER = "#1f2733";
const TEXT = "#e6edf3";
const DIM = "#8b949e";
const GREEN = "#4ade80";
const CYAN = "#38bdf8";
const RED = "#f87171";
const MONO =
  "ui-monospace, 'SF Mono', 'Cascadia Code', 'Fira Code', Menlo, monospace";

const Vignette: React.FC = () => (
  <AbsoluteFill
    style={{
      background:
        "radial-gradient(ellipse at 50% 40%, rgba(56,189,248,0.07), transparent 55%), radial-gradient(ellipse at 50% 110%, rgba(74,222,128,0.06), transparent 50%)",
    }}
  />
);

const Typed: React.FC<{
  text: string;
  startFrame: number;
  charsPerFrame?: number;
  cursor?: boolean;
  style?: React.CSSProperties;
}> = ({ text, startFrame, charsPerFrame = 1.2, cursor = true, style }) => {
  const frame = useCurrentFrame();
  const visible = Math.max(0, Math.floor((frame - startFrame) * charsPerFrame));
  const done = visible >= text.length;
  const blink = Math.floor(frame / 15) % 2 === 0;
  return (
    <span style={{ fontFamily: MONO, whiteSpace: "pre", ...style }}>
      {text.slice(0, visible)}
      {cursor && (!done || blink) && frame >= startFrame ? (
        <span style={{ color: GREEN }}>▊</span>
      ) : null}
    </span>
  );
};

const FadeUp: React.FC<{
  delay: number;
  children: React.ReactNode;
  style?: React.CSSProperties;
}> = ({ delay, children, style }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const progress = spring({ frame: frame - delay, fps, config: { damping: 200 } });
  return (
    <div
      style={{
        opacity: progress,
        transform: `translateY(${interpolate(progress, [0, 1], [40, 0])}px)`,
        ...style,
      }}
    >
      {children}
    </div>
  );
};

const SceneFade: React.FC<{
  children: React.ReactNode;
  fadeOutStart?: number;
  duration: number;
}> = ({ children, fadeOutStart, duration }) => {
  const frame = useCurrentFrame();
  const start = fadeOutStart ?? duration - 15;
  const opacity = interpolate(frame, [0, 12, start, duration], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return <AbsoluteFill style={{ opacity }}>{children}</AbsoluteFill>;
};

// Scene 1 — title
const TitleScene: React.FC = () => (
  <SceneFade duration={120}>
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", gap: 36 }}
    >
      <Typed
        text="Coding Tools MCP"
        startFrame={8}
        charsPerFrame={0.55}
        style={{ fontSize: 110, fontWeight: 700, color: TEXT }}
      />
      <FadeUp delay={55}>
        <div style={{ fontSize: 40, color: DIM, fontFamily: MONO }}>
          Give any AI a safe pair of hands on your codebase.
        </div>
      </FadeUp>
    </AbsoluteFill>
  </SceneFade>
);

// Scene 2 — one server, every client
const CLIENTS = ["Claude Desktop", "Claude Code", "Cursor", "Cline"];

const ClientsScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return (
    <SceneFade duration={240}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <div style={{ display: "flex", gap: 56, marginBottom: 90 }}>
          {CLIENTS.map((name, i) => {
            const p = spring({
              frame: frame - 15 - i * 10,
              fps,
              config: { damping: 200 },
            });
            return (
              <div
                key={name}
                style={{
                  opacity: p,
                  transform: `translateY(${interpolate(p, [0, 1], [-60, 0])}px)`,
                  border: `2px solid ${BORDER}`,
                  background: PANEL,
                  color: TEXT,
                  borderRadius: 18,
                  padding: "26px 44px",
                  fontSize: 38,
                  fontFamily: MONO,
                }}
              >
                {name}
              </div>
            );
          })}
        </div>
        <FadeUp delay={62}>
          <div style={{ fontSize: 46, color: DIM, marginBottom: 90 }}>
            ⌄ ⌄ ⌄ ⌄
          </div>
        </FadeUp>
        <FadeUp delay={75}>
          <div
            style={{
              border: `3px solid ${GREEN}`,
              boxShadow: `0 0 60px rgba(74,222,128,0.25)`,
              background: PANEL,
              color: GREEN,
              borderRadius: 22,
              padding: "34px 70px",
              fontSize: 54,
              fontFamily: MONO,
              fontWeight: 700,
            }}
          >
            coding-tools-mcp
          </div>
        </FadeUp>
        <FadeUp delay={100}>
          <div style={{ fontSize: 40, color: TEXT, marginTop: 70 }}>
            One MCP server · every AI client · the same 20 tools
          </div>
        </FadeUp>
      </AbsoluteFill>
    </SceneFade>
  );
};

// Scene 3 — terminal demo
const TOOL_LINES = [
  { at: 95, text: "→ read_file        src/app.py", ok: "✓" },
  { at: 125, text: "→ apply_patch      3 files, atomic + rollback", ok: "✓" },
  { at: 155, text: "→ exec_command     pytest -q", ok: "✓ 42 passed" },
  { at: 185, text: "→ git_diff         review before you trust", ok: "✓" },
];

const TerminalScene: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <SceneFade duration={240}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <div
          style={{
            width: 1420,
            background: PANEL,
            border: `2px solid ${BORDER}`,
            borderRadius: 20,
            overflow: "hidden",
            boxShadow: "0 30px 90px rgba(0,0,0,0.55)",
          }}
        >
          <div
            style={{
              display: "flex",
              gap: 14,
              padding: "20px 26px",
              borderBottom: `2px solid ${BORDER}`,
            }}
          >
            {[RED, "#fbbf24", GREEN].map((c) => (
              <div
                key={c}
                style={{ width: 22, height: 22, borderRadius: 11, background: c }}
              />
            ))}
          </div>
          <div style={{ padding: 44, fontSize: 34, lineHeight: 1.85 }}>
            <div>
              <span style={{ color: DIM, fontFamily: MONO }}>$ </span>
              <Typed
                text="uvx coding-tools-mcp --stdio --workspace ./repo"
                startFrame={12}
                style={{ color: TEXT }}
              />
            </div>
            {frame > 70 ? (
              <div style={{ color: DIM, fontFamily: MONO }}>
                listening on stdio · workspace confined · permission mode: safe
              </div>
            ) : null}
            {TOOL_LINES.map((line) =>
              frame > line.at ? (
                <div key={line.text} style={{ fontFamily: MONO }}>
                  <span style={{ color: CYAN }}>{line.text}</span>
                  <span style={{ color: GREEN }}>  {line.ok}</span>
                </div>
              ) : null
            )}
          </div>
        </div>
        <FadeUp delay={200}>
          <div style={{ fontSize: 40, color: TEXT, marginTop: 60 }}>
            Read · Patch · Run · Git — token-frugal results, built for context
            windows
          </div>
        </FadeUp>
      </AbsoluteFill>
    </SceneFade>
  );
};

// Scene 4 — safety
const MODES = [
  { name: "safe", color: GREEN, note: "default — everything risky asks first" },
  { name: "trusted", color: CYAN, note: "local dev — network + scripts open" },
  { name: "dangerous", color: RED, note: "isolated containers only" },
];

const SafetyScene: React.FC = () => (
  <SceneFade duration={270}>
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", gap: 54 }}
    >
      <FadeUp delay={8}>
        <div style={{ fontSize: 72, fontWeight: 700, color: TEXT }}>
          Safety is the product.
        </div>
      </FadeUp>
      <div style={{ display: "flex", gap: 44 }}>
        {MODES.map((mode, i) => (
          <FadeUp key={mode.name} delay={35 + i * 14}>
            <div
              style={{
                border: `3px solid ${mode.color}`,
                background: PANEL,
                borderRadius: 20,
                padding: "34px 44px",
                width: 460,
                height: 300,
              }}
            >
              <div
                style={{
                  fontSize: 58,
                  fontFamily: MONO,
                  fontWeight: 700,
                  color: mode.color,
                  marginBottom: 10,
                }}
              >
                {mode.name}
              </div>
              <div
                style={{
                  fontSize: 24,
                  fontFamily: MONO,
                  color: DIM,
                  marginBottom: 20,
                }}
              >
                --permission-mode {mode.name}
              </div>
              <div style={{ fontSize: 30, color: DIM, lineHeight: 1.55 }}>
                {mode.note}
              </div>
            </div>
          </FadeUp>
        ))}
      </div>
      <FadeUp delay={110}>
        <div
          style={{
            fontSize: 36,
            color: TEXT,
            textAlign: "center",
            lineHeight: 1.8,
          }}
        >
          One workspace root — no path escapes, no symlink tricks.
          <br />
          Gates on network, shell expansion, inline scripts, destructive
          commands.
          <br />
          <span style={{ color: GREEN }}>
            Linux Landlock: kernel-level filesystem confinement.
          </span>
        </div>
      </FadeUp>
    </AbsoluteFill>
  </SceneFade>
);

// Scene 5 — unique tricks
const TRICKS = [
  {
    title: "Code from anywhere",
    cmd: "./scripts/tunnel.sh cloudflared ~/repo",
    note: "authenticated HTTPS tunnel to your own machine — drive it from your phone",
  },
  {
    title: "Disposable sandbox",
    cmd: "docker run … coding-tools-mcp-sandbox",
    note: "point an agent at untrusted code, then destroy the container",
  },
  {
    title: "Cloud sandbox via MCP",
    cmd: "start_coding_tools_sandbox()",
    note: "one tool call boots a tunnel-backed runner on GitHub Actions",
  },
];

const TricksScene: React.FC = () => (
  <SceneFade duration={270}>
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", gap: 60 }}
    >
      <FadeUp delay={8}>
        <div style={{ fontSize: 68, fontWeight: 700, color: TEXT }}>
          And the tricks nobody else has:
        </div>
      </FadeUp>
      <div style={{ display: "flex", gap: 42 }}>
        {TRICKS.map((trick, i) => (
          <FadeUp key={trick.title} delay={35 + i * 18}>
            <div
              style={{
                border: `2px solid ${BORDER}`,
                background: PANEL,
                borderRadius: 20,
                padding: "36px 40px",
                width: 500,
                height: 440,
              }}
            >
              <div
                style={{
                  fontSize: 42,
                  fontWeight: 700,
                  color: TEXT,
                  marginBottom: 24,
                }}
              >
                {trick.title}
              </div>
              <div
                style={{
                  fontSize: 28,
                  fontFamily: MONO,
                  color: CYAN,
                  marginBottom: 24,
                }}
              >
                {trick.cmd}
              </div>
              <div style={{ fontSize: 29, color: DIM, lineHeight: 1.6 }}>
                {trick.note}
              </div>
            </div>
          </FadeUp>
        ))}
      </div>
    </AbsoluteFill>
  </SceneFade>
);

// Scene 6 — CTA
const CtaScene: React.FC = () => (
  <SceneFade duration={210} fadeOutStart={195}>
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", gap: 48 }}
    >
      <FadeUp delay={5}>
        <div style={{ fontSize: 40, color: DIM, fontFamily: MONO }}>
          −37% tool-result bytes · 20 tools · Apache-2.0 · PyPI + npm
        </div>
      </FadeUp>
      <FadeUp delay={30}>
        <div
          style={{
            fontSize: 88,
            fontFamily: MONO,
            fontWeight: 700,
            color: GREEN,
            border: `3px solid ${GREEN}`,
            borderRadius: 24,
            padding: "36px 80px",
            boxShadow: "0 0 80px rgba(74,222,128,0.25)",
          }}
        >
          npx coding-tools-mcp
        </div>
      </FadeUp>
      <FadeUp delay={60}>
        <div style={{ fontSize: 44, color: TEXT, fontFamily: MONO }}>
          github.com/xyTom/coding-tools-mcp
        </div>
      </FadeUp>
      <FadeUp delay={85}>
        <div style={{ fontSize: 36, color: DIM }}>
          ★ Star it if it saves you tokens.
        </div>
      </FadeUp>
    </AbsoluteFill>
  </SceneFade>
);

export const Promo: React.FC = () => {
  return (
    <AbsoluteFill style={{ background: BG, color: TEXT, fontFamily: MONO }}>
      <Vignette />
      <Sequence durationInFrames={120}>
        <TitleScene />
      </Sequence>
      <Sequence from={120} durationInFrames={240}>
        <ClientsScene />
      </Sequence>
      <Sequence from={360} durationInFrames={240}>
        <TerminalScene />
      </Sequence>
      <Sequence from={600} durationInFrames={270}>
        <SafetyScene />
      </Sequence>
      <Sequence from={870} durationInFrames={270}>
        <TricksScene />
      </Sequence>
      <Sequence from={1140} durationInFrames={210}>
        <CtaScene />
      </Sequence>
    </AbsoluteFill>
  );
};
