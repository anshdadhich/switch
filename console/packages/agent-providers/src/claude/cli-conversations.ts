import { execFile } from 'node:child_process';
import { readFile, readdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import {
  forkSession as sdkForkSession,
  listSessions as sdkListSessions,
} from '@anthropic-ai/claude-agent-sdk';
import { z } from 'zod';

/** The Claude Code conversations started in a folder outside Switch, newest first. */
export const cliConversationListSchema = z.object({
  /**
   * Whether this Claude Code records which conversations its running processes
   * have open. When it does not, an open conversation looks like a closed one.
   */
  ownershipKnown: z.boolean(),
  conversations: z.array(
    z.object({
      nativeSessionId: z.string(),
      title: z.string(),
      lastActivityAt: z.number(),
      gitBranch: z.string().nullable(),
      /** The running Claude Code process that has the conversation open, or null for none. */
      openIn: z.object({ pid: z.number(), entrypoint: z.string().nullable() }).nullable(),
    })
  ),
});
export type CliConversationList = z.infer<typeof cliConversationListSchema>;

export const forkedCliConversationSchema = z.object({ nativeSessionId: z.string() });

/** What a fork is titled in Claude Code's own `/resume` list, so it reads as taken. */
const FORK_TITLE_SUFFIX = ' (continued in Switch Console)';

/** One running Claude Code process's own record, under its config directory's `sessions/<pid>.json`. */
const holderRecordSchema = z.object({
  pid: z.number().int().positive(),
  sessionId: z.string(),
  procStart: z.string().optional(),
  entrypoint: z.string().optional(),
});

/** `ps -o lstart=` as Claude Code records it: `Mon Oct  6 18:02:59 2026`, in UTC. */
const PROCESS_START = /^[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}$/;

type Holder = { pid: number; entrypoint: string | null };

export interface CliConversationDeps {
  listSessions: typeof sdkListSessions;
  forkSession: typeof sdkForkSession;
  /** When the process with this id started, as `ps -o lstart=` prints it in UTC; null when unknown. */
  processStart: (pid: number) => Promise<string | null>;
  processExists: (pid: number) => boolean;
}

const defaultDeps: CliConversationDeps = {
  listSessions: sdkListSessions,
  forkSession: sdkForkSession,
  processStart,
  processExists,
};

/**
 * The conversations started in `dir` from Claude Code's own terminal or IDE
 * clients, with the process that has each one open. Conversations a program
 * started through the SDK, Switch's own sessions among them, are left out.
 */
export async function listClaudeCliConversations(
  dir: string,
  env: Record<string, string | undefined>,
  deps: CliConversationDeps = defaultDeps
): Promise<CliConversationList> {
  const [sessions, holders] = await Promise.all([
    deps.listSessions({ dir, includeProgrammatic: false, includeWorktrees: false }),
    claudeHolders(configDir(env), deps),
  ]);
  return {
    ownershipKnown: holders !== null,
    conversations: [...sessions]
      .sort((a, b) => b.lastModified - a.lastModified)
      .map((session) => ({
        nativeSessionId: session.sessionId,
        title: session.customTitle || session.summary,
        lastActivityAt: session.lastModified,
        gitBranch: session.gitBranch ?? null,
        openIn: holders?.get(session.sessionId) ?? null,
      })),
  };
}

/**
 * A copy of a Claude Code conversation under a new id, for a Switch session to
 * continue: the original stays the terminal's, and nothing Switch does is
 * written to it. Refused while a running Claude Code process has it open, so
 * the copy is never taken from the middle of a turn.
 */
export async function forkClaudeCliConversation(
  dir: string,
  nativeSessionId: string,
  env: Record<string, string | undefined>,
  deps: CliConversationDeps = defaultDeps
): Promise<{ nativeSessionId: string }> {
  const holder = (await claudeHolders(configDir(env), deps))?.get(nativeSessionId);
  if (holder)
    throw new Error(
      `This conversation is open in Claude Code (process ${holder.pid}). Close it there, then try again.`
    );
  const listed = await deps.listSessions({
    dir,
    includeProgrammatic: false,
    includeWorktrees: false,
  });
  const source = listed.find((session) => session.sessionId === nativeSessionId);
  if (!source)
    throw new Error(`Claude Code has no conversation ${nativeSessionId} in ${dir} to continue.`);
  const title = (source.customTitle || source.summary).slice(0, 200);
  const fork = await deps.forkSession(nativeSessionId, {
    dir,
    title: `${title}${FORK_TITLE_SUFFIX}`,
  });
  return { nativeSessionId: fork.sessionId };
}

function configDir(env: Record<string, string | undefined>): string {
  return env.CLAUDE_CONFIG_DIR || join(env.HOME || homedir(), '.claude');
}

/**
 * The conversations Claude Code's running processes have open, by
 * conversation id, from the record each process keeps. Null when there are no
 * records at all: a Claude Code too old to keep them.
 *
 * A record is only as good as its process. One whose process has gone holds
 * nothing; so does one whose process id now names a process that started at
 * another time. A live process whose start cannot be compared — no `ps`, or a
 * record in a shape this does not know — is taken to hold its conversation.
 */
async function claudeHolders(
  dir: string,
  deps: CliConversationDeps
): Promise<Map<string, Holder> | null> {
  let names: string[];
  try {
    names = await readdir(join(dir, 'sessions'));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
    throw error;
  }
  const holders = new Map<string, Holder>();
  for (const name of names) {
    if (!name.endsWith('.json')) continue;
    let record: z.infer<typeof holderRecordSchema>;
    try {
      record = holderRecordSchema.parse(
        JSON.parse(await readFile(join(dir, 'sessions', name), 'utf8'))
      );
    } catch (error) {
      // Its process may be writing it; it is read again on the next listing.
      console.warn(`Skipped Claude Code's process record ${name}: ${String(error)}`);
      continue;
    }
    if (!deps.processExists(record.pid)) continue;
    const recorded = record.procStart?.trim().replace(/\s+/g, ' ');
    if (recorded && PROCESS_START.test(recorded)) {
      const actual = await deps.processStart(record.pid);
      if (actual && PROCESS_START.test(actual) && actual !== recorded) continue;
    }
    holders.set(record.sessionId, { pid: record.pid, entrypoint: record.entrypoint ?? null });
  }
  return holders;
}

function processExists(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM: it exists, as another user's.
    return (error as NodeJS.ErrnoException).code !== 'ESRCH';
  }
}

async function processStart(pid: number): Promise<string | null> {
  if (process.platform === 'win32') return null;
  try {
    const { stdout } = await promisify(execFile)('ps', ['-o', 'lstart=', '-p', String(pid)], {
      encoding: 'utf8',
      env: { ...process.env, TZ: 'UTC', LC_ALL: 'C' },
    });
    return stdout.trim().replace(/\s+/g, ' ') || null;
  } catch {
    return null;
  }
}
