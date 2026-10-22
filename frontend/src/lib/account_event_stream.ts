// The account's event stream, `GET /api/v1/events/stream` (PLAN.md section 8.1), read with fetch because EventSource
// cannot send Last-Event-ID on the first connect
// (https://developer.mozilla.org/en-US/docs/Web/API/EventSource/EventSource). Reconnects with the last id seen (ids are
// random `evt_...`, so the last one received, not the largest); the caller dedupes by event id. A session holds a few streams at once; past that the API answers 429
// `too_many_event_streams` and the stream stops in the `refused` state instead of retrying, since another retry would
// be refused the same way until a tab closes.
import { ApiError } from "@/lib/api_client"
import type { AccountEvent } from "@/lib/api_types"

const defaultReconnectDelayMilliseconds = 3000
const maximumReconnectDelayMilliseconds = 30000

export type EventStreamConnectionState = "connecting" | "live" | "reconnecting" | "signed_out" | "refused"

export interface AccountEventStreamHandlers {
  onAccountEvent(accountEvent: AccountEvent, isCaughtUp: boolean): void
  onCaughtUp(latestEventId: string | null): void
  onConnectionStateChange(connectionState: EventStreamConnectionState): void
  // The API's refusal that put the stream in the `refused` state.
  onStreamRefused(refusal: ApiError): void
}

interface ServerSentEventBlock {
  event: string
  dataLines: string[]
  retry: number | null
}

// One "field: value" block; comments (": ping") and unknown fields are skipped.
function parseServerSentEventBlock(blockText: string): ServerSentEventBlock {
  const message: ServerSentEventBlock = { event: "message", dataLines: [], retry: null }
  for (const line of blockText.split("\n")) {
    if (!line || line.startsWith(":")) continue
    const separatorIndex = line.indexOf(":")
    const fieldName = separatorIndex === -1 ? line : line.slice(0, separatorIndex)
    const fieldValue = separatorIndex === -1 ? "" : line.slice(separatorIndex + 1).replace(/^ /, "")
    if (fieldName === "event") message.event = fieldValue
    else if (fieldName === "data") message.dataLines.push(fieldValue)
    else if (fieldName === "retry" && /^\d+$/.test(fieldValue)) message.retry = Number(fieldValue)
  }
  return message
}

class SignedOutError extends Error {}

// A 429's JSON body as an ApiError, so the page can say it with describeError.
async function readRateLimitRefusal(response: Response): Promise<ApiError> {
  let errorBody: { message?: string; code?: string } = {}
  try {
    errorBody = (await response.json()) ?? {}
  } catch {
    errorBody = {}
  }
  const retryAfterHeader = response.headers.get("Retry-After")
  const retryAfterSeconds = retryAfterHeader && /^\d+$/.test(retryAfterHeader) ? Number(retryAfterHeader) : null
  return new ApiError(errorBody.message ?? `The server answered ${response.status}.`, errorBody.code ?? null, response.status, retryAfterSeconds)
}

// Follows the stream until `abortSignal` fires or the session ends.
export async function followAccountEventStream(abortSignal: AbortSignal, handlers: AccountEventStreamHandlers): Promise<void> {
  let lastEventId: string | null = null
  let isCaughtUp = false
  let reconnectDelayMilliseconds = defaultReconnectDelayMilliseconds

  async function readOneConnection(): Promise<number | null> {
    const headers: Record<string, string> = { Accept: "text/event-stream" }
    if (lastEventId !== null) headers["Last-Event-ID"] = lastEventId
    const response = await fetch("/api/v1/events/stream", { headers, cache: "no-store", signal: abortSignal })
    if (response.status === 401) throw new SignedOutError("signed out")
    if (response.status === 429) throw await readRateLimitRefusal(response)
    if (!response.ok || !response.body) throw new Error(`The server answered ${response.status}.`)
    handlers.onConnectionStateChange("live")
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
    let bufferedText = ""
    let retryMilliseconds: number | null = null
    for (;;) {
      const { value, done } = await reader.read()
      if (done) return retryMilliseconds
      bufferedText = (bufferedText + value).replace(/\r\n?/g, "\n")
      let blockEnd = bufferedText.indexOf("\n\n")
      while (blockEnd !== -1) {
        const message = parseServerSentEventBlock(bufferedText.slice(0, blockEnd))
        bufferedText = bufferedText.slice(blockEnd + 2)
        blockEnd = bufferedText.indexOf("\n\n")
        if (message.retry !== null) retryMilliseconds = message.retry
        if (message.dataLines.length === 0) continue
        const payload = JSON.parse(message.dataLines.join("\n"))
        if (message.event === "account_event") {
          const accountEvent = payload as AccountEvent
          lastEventId = accountEvent.id
          handlers.onAccountEvent(accountEvent, isCaughtUp)
        } else if (message.event === "caught_up") {
          isCaughtUp = true
          if (typeof payload.latest_event_id === "string") lastEventId = payload.latest_event_id
          handlers.onCaughtUp(lastEventId)
        }
      }
    }
  }

  handlers.onConnectionStateChange("connecting")
  while (!abortSignal.aborted) {
    try {
      const retryMilliseconds = await readOneConnection()
      reconnectDelayMilliseconds = retryMilliseconds ?? defaultReconnectDelayMilliseconds
    } catch (streamError) {
      if (abortSignal.aborted) return
      if (streamError instanceof SignedOutError) {
        handlers.onConnectionStateChange("signed_out")
        return
      }
      if (streamError instanceof ApiError && streamError.code === "too_many_event_streams") {
        handlers.onStreamRefused(streamError)
        handlers.onConnectionStateChange("refused")
        return
      }
      reconnectDelayMilliseconds = Math.min(reconnectDelayMilliseconds * 2, maximumReconnectDelayMilliseconds)
    }
    if (abortSignal.aborted) return
    handlers.onConnectionStateChange("reconnecting")
    await new Promise((resolve) => setTimeout(resolve, reconnectDelayMilliseconds))
  }
}
