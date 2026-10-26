import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { SDKSessionInfo } from '@anthropic-ai/claude-agent-sdk';
import { afterEach, expect, it, vi } from 'vitest';
import {
  type CliConversationDeps,
  forkClaudeCliConversation,
  listClaudeCliConversations,
} from './cli-conversations';

const roots: string[] = [];
afterEach(async () => {
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true });
});

const STARTED = 'Mon Oct 26 18:02:59 2026';

const SESSIONS: SDKSessionInfo[] = [
  { sessionId: 'older', summary: 'Tidy the README', lastModified: 1_000, gitBranch: 'main' },
  {
    sessionId: 'newer',
    summary: 'Fix the build',
    customTitle: 'Build fix',
    lastModified: 2_000,
  },
];

/** A Claude Code config directory, with a process record for each of `holders`. */
async function configDir(
  holders: { pid: number; sessionId: string; procStart?: string }[] | null
): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), 'claude-config-'));
  roots.push(dir);
  if (holders) {
    await mkdir(join(dir, 'sessions'));
    for (const holder of holders)
      await writeFile(
        join(dir, 'sessions', `${holder.pid}.json`),
        JSON.stringify({ ...holder, entrypoint: 'cli', status: 'idle' })
      );
  }
  return dir;
}

function deps(overrides: Partial<CliConversationDeps> = {}): CliConversationDeps {
  return {
    listSessions: vi.fn(async () => structuredClone(SESSIONS)),
    forkSession: vi.fn(async () => ({ sessionId: 'copy' })),
    processExists: () => true,
    processStart: async () => STARTED,
    ...overrides,
  };
}

it('lists the conversations started from Claude Code’s own clients, newest first, with what holds each', async () => {
  const dir = await configDir([{ pid: 41, sessionId: 'newer', procStart: STARTED }]);
  const run = deps();
  const list = await listClaudeCliConversations('/work', { CLAUDE_CONFIG_DIR: dir }, run);

  expect(run.listSessions).toHaveBeenCalledWith({
    dir: '/work',
    includeProgrammatic: false,
    includeWorktrees: false,
  });
  expect(list).toEqual({
    ownershipKnown: true,
    conversations: [
      {
        nativeSessionId: 'newer',
        title: 'Build fix',
        lastActivityAt: 2_000,
        gitBranch: null,
        openIn: { pid: 41, entrypoint: 'cli' },
      },
      {
        nativeSessionId: 'older',
        title: 'Tidy the README',
        lastActivityAt: 1_000,
        gitBranch: 'main',
        openIn: null,
      },
    ],
  });
});

it('does not count a record whose process has gone, or whose process id now names another process', async () => {
  const dir = await configDir([
    { pid: 41, sessionId: 'newer', procStart: STARTED },
    { pid: 42, sessionId: 'older', procStart: STARTED },
  ]);
  const list = await listClaudeCliConversations(
    '/work',
    { CLAUDE_CONFIG_DIR: dir },
    deps({
      processExists: (pid) => pid === 42,
      processStart: async () => 'Tue Oct 27 09:00:00 2026',
    })
  );
  expect(list.conversations.map((c) => c.openIn)).toEqual([null, null]);
});

it('takes a live process to hold its conversation when its start cannot be compared', async () => {
  const dir = await configDir([
    { pid: 41, sessionId: 'newer', procStart: STARTED },
    { pid: 42, sessionId: 'older', procStart: '2026-10-26T18:02:59Z' },
  ]);
  const list = await listClaudeCliConversations(
    '/work',
    { CLAUDE_CONFIG_DIR: dir },
    deps({ processStart: async (pid) => (pid === 41 ? null : 'Tue Oct 27 09:00:00 2026') })
  );
  expect(list.conversations.map((c) => c.openIn?.pid)).toEqual([41, 42]);
});

it('says when this Claude Code keeps no record of what its processes hold', async () => {
  const dir = await configDir(null);
  const list = await listClaudeCliConversations('/work', { CLAUDE_CONFIG_DIR: dir }, deps());
  expect(list.ownershipKnown).toBe(false);
  expect(list.conversations.every((c) => c.openIn === null)).toBe(true);
});

it('copies a closed conversation under a new id, titled so Claude Code’s own list shows it is taken', async () => {
  const dir = await configDir([]);
  const run = deps();
  await expect(
    forkClaudeCliConversation('/work', 'newer', { CLAUDE_CONFIG_DIR: dir }, run)
  ).resolves.toEqual({ nativeSessionId: 'copy' });
  expect(run.forkSession).toHaveBeenCalledWith('newer', {
    dir: '/work',
    title: 'Build fix (continued in Switch Console)',
  });
});

it('refuses to copy a conversation that is open in Claude Code', async () => {
  const dir = await configDir([{ pid: 41, sessionId: 'newer', procStart: STARTED }]);
  const run = deps();
  await expect(
    forkClaudeCliConversation('/work', 'newer', { CLAUDE_CONFIG_DIR: dir }, run)
  ).rejects.toThrow('This conversation is open in Claude Code (process 41)');
  expect(run.forkSession).not.toHaveBeenCalled();
});

it('refuses a conversation that is not one of the folder’s own', async () => {
  const dir = await configDir([]);
  const run = deps();
  await expect(
    forkClaudeCliConversation('/work', 'elsewhere', { CLAUDE_CONFIG_DIR: dir }, run)
  ).rejects.toThrow('Claude Code has no conversation elsewhere in /work to continue.');
  expect(run.forkSession).not.toHaveBeenCalled();
});
