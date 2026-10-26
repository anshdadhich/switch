import { z } from 'zod';
import {
  type CliConversationList,
  forkClaudeCliConversation,
  listClaudeCliConversations,
} from '../claude/cli-conversations';

/**
 * What the host's one-off conversation commands print instead of their answer
 * when they fail, so Console can say why: the conversation is open in the
 * terminal, say, rather than that a command exited 1.
 */
export const cliConversationFailureSchema = z.object({ error: z.string() });

/** The conversations started in `dir` from the provider's own CLI, for a session to continue. */
export async function listCliConversations(
  provider: string,
  dir: string
): Promise<CliConversationList> {
  continuable(provider);
  return listClaudeCliConversations(dir, process.env);
}

/** A copy of one of those conversations, under a new id, for a session to continue. */
export async function forkCliConversation(
  provider: string,
  dir: string,
  nativeSessionId: string
): Promise<{ nativeSessionId: string }> {
  continuable(provider);
  return forkClaudeCliConversation(dir, nativeSessionId, process.env);
}

function continuable(provider: string): void {
  if (provider !== 'claude')
    throw new Error(
      `Switch continues conversations started in Claude Code, not ones started in ${provider}.`
    );
}
