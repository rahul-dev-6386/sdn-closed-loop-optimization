/**
 * Edge Payload Validation Schemas (Zod).
 *
 * Implements Ground Rule 0:
 * - Validate every incoming payload at the edge.
 * - Unknown fields are ignored.
 * - Invalid payloads become a "bad data" state, never 0.
 */
import { z } from 'zod'

export const HostSchema = z.object({
  name: z.string(),
  ip: z.string().default('n/a'),
  switch: z.string(),
  x: z.number().optional(),
  y: z.number().optional(),
})

export const LinkSchema = z.object({
  a: z.string(),
  b: z.string(),
  bw_mbps: z.coerce.number().positive().default(10.0),
  delay_ms: z.coerce.number().nonnegative().optional().default(2.0),
  host_link: z.boolean().optional().default(false),
})

export const TopologyResponseSchema = z.object({
  id: z.string().optional(),
  name: z.string().default('Unknown Topology'),
  category: z.string().optional(),
  description: z.string().optional(),
  switches: z.array(z.string()).default([]),
  hosts: z.array(HostSchema).default([]),
  links: z.array(LinkSchema).default([]),
  default_path: z.array(z.string()).optional(),
  defaultPath: z.array(z.string()).optional(),
  alternate_path: z.array(z.string()).optional(),
  alternatePath: z.array(z.string()).optional(),
  bottleneck_link: z.string().optional(),
  bottleneckLink: z.string().optional(),
  node_coordinates: z.record(z.string(), z.object({
    x: z.number(),
    y: z.number(),
    layer: z.number().optional(),
  })).optional(),
}).passthrough()

export const SdnEventSchema = z.object({
  id: z.string().optional(),
  seq: z.number().optional(),
  kind: z.string().default('unknown'),
  summary: z.string().default(''),
  wall_iso: z.string().optional(),
  timestamp: z.number().optional(),
  elapsed_s: z.number().optional(),
  link: z.string().optional(),
  rate_mbps: z.number().optional(),
}).passthrough()

export const LiveFrameSchema = z.object({
  type: z.string().default('snapshot'),
  at: z.number().default(() => Date.now() / 1000),
  topology: z.any().optional(),
  controller: z.object({
    listening: z.boolean().default(false),
    connected: z.boolean().default(false),
    datapaths: z.array(z.string()).default([]),
  }).passthrough().optional(),
  events: z.array(SdnEventSchema).default([]),
  telemetry: z.array(z.record(z.string(), z.any())).optional().default([]),
  fabric: z.record(z.string(), z.any()).optional().default({}),
}).passthrough()

export const FlowTableEntrySchema = z.object({
  available: z.boolean().default(true),
  reason: z.string().optional(),
  switches: z.record(z.string(), z.object({
    count: z.number().default(0),
    forwarding: z.number().default(0),
    entries: z.array(z.string()).default([]),
  })).default({}),
}).passthrough()

/**
 * Safely parses and validates arbitrary data with fallback to 'bad_data'.
 */
export function safeValidate<T>(
  schema: z.ZodSchema<T>,
  data: unknown,
  fallbackDesc: string,
): { success: true; data: T } | { success: false; error: string } {
  const result = schema.safeParse(data)
  if (!result.success) {
    if (import.meta.env.DEV) {
      console.warn(`[EdgeValidation] Failed to validate ${fallbackDesc}:`, result.error)
    }
    return { success: false, error: `Invalid ${fallbackDesc} payload: ${result.error.issues[0]?.message ?? 'Malformed'}` }
  }
  return { success: true, data: result.data }
}
