import {
  cliConversationFailureSchema,
  cliConversationListSchema,
  forkedCliConversationSchema,
} from '@switch-console/agent-providers';
import { err, ok, type Result } from '@switch-console/shared';
import { eq } from 'drizzle-orm';
import type { z } from 'zod';
import { getAgentById } from '@main/core/agents/getAgentById';
import { locationManager } from '@main/core/locations/location-manager';
import type { LocationProvider } from '@main/core/locations/location-provider';
import { runLaunchHostCommand } from '@main/core/sdk-host/shared-host-deployment';
import { db } from '@main/db/client';
import { agents, sessions } from '@main/db/schema';
import type { CliConversationList } from '@shared/core/sessions/cli-conversations';

/**
 * The conversations started in Claude Code's own CLI in an agent's folder that
 * a new session could continue, read on the machine the agent's sessions run
 * on. The copies Console's own sessions already run are left out.
 */
export async function listCliConversations(
  agentId: string
): Promise<Result<CliConversationList, { message: string }>> {
  try {
    const agent = await getAgentById(agentId);
    if (!agent) throw new Error('This agent no longer exists.');
    if (agent.providerId !== 'claude')
      throw new Error('Only Claude Code agents can continue a conversation started in its CLI.');
    const location = locationManager.getLocation(agent.locationId);
    if (!location) throw new Error('Open the agent’s folder in Console first.');
    const [list, continued] = await Promise.all([
      runConversationCommand(
        location,
        ['--cli-conversations', 'claude', location.dir],
        cliConversationListSchema
      ),
      continuedConversations(agent.locationId),
    ]);
    return ok({
      ...list,
      conversations: list.conversations.filter(
        (conversation) => !continued.has(conversation.nativeSessionId)
      ),
    });
  } catch (error) {
    return err({ message: error instanceof Error ? error.message : String(error) });
  }
}

/**
 * Copies a conversation started in Claude Code's CLI for a session to continue,
 * and returns the copy's id. Refused while a Claude Code process has the
 * conversation open.
 */
export async function forkCliConversation(
  location: LocationProvider,
  nativeSessionId: string
): Promise<string> {
  const fork = await runConversationCommand(
    location,
    ['--fork-cli-conversation', 'claude', location.dir, nativeSessionId],
    forkedCliConversationSchema
  );
  return fork.nativeSessionId;
}

/**
 * Runs one of the shared host's conversation commands where the location's
 * sessions run. A command that fails prints why on stdout; that reason is what
 * is thrown, rather than that the command exited 1.
 */
async function runConversationCommand<T>(
  location: LocationProvider,
  args: string[],
  schema: z.ZodType<T>
): Promise<T> {
  let stdout: string;
  try {
    ({ stdout } = await runLaunchHostCommand(location.transport, args));
  } catch (error) {
    const failure = cliConversationFailureSchema.safeParse(lastJsonLine(error));
    if (failure.success) throw new Error(failure.data.error);
    throw error;
  }
  const answer = schema.safeParse(lastJsonLine({ stdout }));
  if (!answer.success)
    throw new Error(
      `Could not read what the agent's host answered: ${stdout.trim().slice(0, 200) || 'it printed nothing'}`
    );
  return answer.data;
}

function lastJsonLine(output: unknown): unknown {
  const stdout = (output as { stdout?: unknown } | null)?.stdout;
  if (typeof stdout !== 'string') return undefined;
  const line = stdout.trim().split('\n').at(-1);
  if (!line) return undefined;
  try {
    return JSON.parse(line);
  } catch {
    return undefined;
  }
}

/** The native conversations Console's sessions in a location already run. */
async function continuedConversations(locationId: string): Promise<Set<string>> {
  const rows = await db
    .select({ config: sessions.config })
    .from(sessions)
    .innerJoin(agents, eq(sessions.agentId, agents.id))
    .where(eq(agents.locationId, locationId));
  return new Set(rows.flatMap((row) => row.config?.providerSessionId ?? []));
}
