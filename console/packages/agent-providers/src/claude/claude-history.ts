import type { SessionMessage } from '@anthropic-ai/claude-agent-sdk';
import type { ConversationHistoryItem, ConversationHistoryTurn } from '../adapter';
import { isRecord, toolTitle } from './claude-mapping';

/**
 * A text that is one tag pair and nothing else, such as
 * `<task-notification>…</task-notification>` or `<ide_opened_file>…`: context
 * Claude Code added to the conversation, not something a person typed.
 */
const ADDED_CONTEXT = /^<([a-z][\w-]*)>[\s\S]*<\/\1>$/;
/** How Claude Code records a slash command someone ran: `<command-name>/mcp</command-name>…`. */
const SLASH_COMMAND = /^<command-name>([^<]*)<\/command-name>/;
const COMMAND_ARGS = /<command-args>([^<]*)<\/command-args>/;

/**
 * A Claude Code conversation as transcript turns: each prompt someone typed
 * starts a turn, and what Claude said and the tools it ran follow it.
 *
 * Reasoning is left out, as it is from a live transcript, and so are a
 * subagent's own messages: the tool call that started the subagent stands for
 * it. A tool whose result was an error is shown as failed.
 */
export function historyFromClaudeMessages(messages: SessionMessage[]): ConversationHistoryTurn[] {
  const turns: ConversationHistoryTurn[] = [];
  const tools = new Map<string, ConversationHistoryItem>();
  let turn: ConversationHistoryTurn | null = null;
  const add = (uuid: string, item: ConversationHistoryItem) => {
    if (!turn) {
      turn = { turnId: `cli:${uuid}`, items: [] };
      turns.push(turn);
    }
    turn.items.push(item);
  };
  for (const message of messages) {
    if (message.parent_tool_use_id !== null) continue;
    const content = isRecord(message.message) ? message.message.content : undefined;
    const blocks =
      typeof content === 'string'
        ? [{ type: 'text', text: content }]
        : Array.isArray(content)
          ? content.filter(isRecord)
          : [];
    if (message.type === 'user') {
      for (const block of blocks)
        if (block.type === 'tool_result' && block.is_error === true) {
          const tool = typeof block.tool_use_id === 'string' && tools.get(block.tool_use_id);
          if (tool) tool.status = 'failed';
        }
      const prompt = blocks
        .map((block) =>
          block.type === 'text' && typeof block.text === 'string'
            ? promptText(block.text)
            : block.type === 'image'
              ? '[image]'
              : ''
        )
        .filter(Boolean)
        .join('\n\n');
      if (!prompt) continue;
      turn = { turnId: `cli:${message.uuid}`, items: [] };
      turns.push(turn);
      add(message.uuid, {
        itemId: `cli:${message.uuid}`,
        kind: 'user-message',
        status: 'completed',
        title: '',
        text: prompt,
      });
    } else if (message.type === 'assistant') {
      blocks.forEach((block, index) => {
        const itemId = `cli:${message.uuid}:${index}`;
        if (block.type === 'text' && typeof block.text === 'string' && block.text.trim())
          add(message.uuid, {
            itemId,
            kind: 'assistant-message',
            status: 'completed',
            title: '',
            text: block.text,
          });
        else if (block.type === 'tool_use' && typeof block.name === 'string') {
          const tool: ConversationHistoryItem = {
            itemId,
            kind: 'tool-activity',
            status: 'completed',
            title: toolTitle(block.name, isRecord(block.input) ? block.input : {}),
            text: '',
          };
          add(message.uuid, tool);
          if (typeof block.id === 'string') tools.set(block.id, tool);
        }
      });
    }
  }
  return turns;
}

/** What a person typed in one text block: a slash command as they typed it, added context as nothing. */
function promptText(text: string): string {
  const trimmed = text.trim();
  const command = SLASH_COMMAND.exec(trimmed);
  if (command) {
    const args = COMMAND_ARGS.exec(trimmed)?.[1]?.trim();
    return [command[1]!.trim(), args].filter(Boolean).join(' ');
  }
  return ADDED_CONTEXT.test(trimmed) ? '' : trimmed;
}
