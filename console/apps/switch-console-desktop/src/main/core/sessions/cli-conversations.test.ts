import { beforeEach, expect, it, vi } from 'vitest';
import { forkCliConversation, listCliConversations } from './cli-conversations';

const mocks = vi.hoisted(() => ({
  getAgentById: vi.fn(),
  getLocation: vi.fn(),
  runLaunchHostCommand: vi.fn(),
  sessionConfigs: vi.fn(),
}));

vi.mock('@main/core/agents/getAgentById', () => ({ getAgentById: mocks.getAgentById }));
vi.mock('@main/core/locations/location-manager', () => ({
  locationManager: { getLocation: mocks.getLocation },
}));
vi.mock('@main/core/sdk-host/shared-host-deployment', () => ({
  runLaunchHostCommand: mocks.runLaunchHostCommand,
}));
vi.mock('@main/db/client', () => ({
  db: {
    select: () => ({
      from: () => ({ innerJoin: () => ({ where: () => mocks.sessionConfigs() }) }),
    }),
  },
}));

const location = { dir: '/work', transport: { kind: 'local' } };

function conversation(nativeSessionId: string) {
  return {
    nativeSessionId,
    title: nativeSessionId,
    lastActivityAt: 1,
    gitBranch: null,
    openIn: null,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getAgentById.mockResolvedValue({ id: 'agent', locationId: 'loc', providerId: 'claude' });
  mocks.getLocation.mockReturnValue(location);
  mocks.sessionConfigs.mockResolvedValue([]);
});

it('lists the folder’s CLI conversations on its host, leaving out the copies Console already runs', async () => {
  mocks.runLaunchHostCommand.mockResolvedValue({
    stdout: `${JSON.stringify({
      ownershipKnown: true,
      conversations: [conversation('cli-1'), conversation('copy-1')],
    })}\n`,
    stderr: '',
  });
  mocks.sessionConfigs.mockResolvedValue([
    { config: { providerSessionId: 'copy-1' } },
    { config: null },
  ]);

  const result = await listCliConversations('agent');

  expect(mocks.runLaunchHostCommand).toHaveBeenCalledWith(location.transport, [
    '--cli-conversations',
    'claude',
    '/work',
  ]);
  expect(result).toEqual({
    success: true,
    data: { ownershipKnown: true, conversations: [conversation('cli-1')] },
  });
});

it('offers nothing for an agent that is not Claude Code', async () => {
  mocks.getAgentById.mockResolvedValue({ id: 'agent', locationId: 'loc', providerId: 'codex' });
  const result = await listCliConversations('agent');
  expect(result).toEqual({
    success: false,
    error: { message: 'Only Claude Code agents can continue a conversation started in its CLI.' },
  });
  expect(mocks.runLaunchHostCommand).not.toHaveBeenCalled();
});

it('throws the reason the host gave for refusing a copy, not that its command failed', async () => {
  mocks.runLaunchHostCommand.mockRejectedValue(
    Object.assign(new Error('Command failed'), {
      stdout: `${JSON.stringify({ error: 'This conversation is open in Claude Code (process 41). Close it there, then try again.' })}\n`,
    })
  );
  await expect(forkCliConversation(location as never, 'cli-1')).rejects.toThrow(
    'This conversation is open in Claude Code (process 41)'
  );
  expect(mocks.runLaunchHostCommand).toHaveBeenCalledWith(location.transport, [
    '--fork-cli-conversation',
    'claude',
    '/work',
    'cli-1',
  ]);
});

it('passes on a failure the host did not explain', async () => {
  const failure = new Error('ssh: connection refused');
  mocks.runLaunchHostCommand.mockRejectedValue(failure);
  await expect(forkCliConversation(location as never, 'cli-1')).rejects.toBe(failure);
});

it('returns the copy’s id', async () => {
  mocks.runLaunchHostCommand.mockResolvedValue({
    stdout: '{"nativeSessionId":"copy-2"}\n',
    stderr: '',
  });
  await expect(forkCliConversation(location as never, 'cli-1')).resolves.toBe('copy-2');
});
