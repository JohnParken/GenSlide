/**
 * In-memory Session Store with TTL, LRU eviction and capacity limits
 */

import { randomUUID } from "node:crypto";
import { SessionError } from "./errors.js";

export interface Session {
  id: string;
  variables: Map<string, string>;
  createdAt: number;
  expiresAt: number;
  lastAccessedAt: number;
  principal?: string;
}

export interface SessionStoreOptions {
  ttlMs: number;
  maxSessions: number;
}

export class SessionStore {
  private sessions = new Map<string, Session>();
  private ttlMs: number;
  private maxSessions: number;
  private cleanupTimer?: NodeJS.Timeout;

  constructor(options: SessionStoreOptions) {
    this.ttlMs = options.ttlMs;
    this.maxSessions = options.maxSessions;

    // Run cleanup periodically (every 1/3 of TTL or 60s, whichever is smaller)
    const intervalMs = Math.min(Math.max(Math.floor(this.ttlMs / 3), 10000), 60000);
    this.cleanupTimer = setInterval(() => this.cleanExpired(), intervalMs);
    // Unref so it won't block node process exit
    if (this.cleanupTimer && typeof this.cleanupTimer.unref === "function") {
      this.cleanupTimer.unref();
    }
  }

  public get size(): number {
    return this.sessions.size;
  }

  public createSession(variables: Record<string, string> | Map<string, string>, principal?: string): Session {
    this.cleanExpired();

    if (this.sessions.size >= this.maxSessions) {
      // Try to evict oldest
      this.evictOldest();
      if (this.sessions.size >= this.maxSessions) {
        throw new SessionError("Session store capacity exceeded", 503);
      }
    }

    const now = Date.now();
    const id = `sess_${randomUUID().replace(/-/g, "")}`;
    const varMap = variables instanceof Map ? new Map(variables) : new Map(Object.entries(variables));

    const session: Session = {
      id,
      variables: varMap,
      createdAt: now,
      expiresAt: now + this.ttlMs,
      lastAccessedAt: now,
      principal,
    };

    this.sessions.set(id, session);
    return session;
  }

  public getSession(id: string): Session | undefined {
    const session = this.sessions.get(id);
    if (!session) {
      return undefined;
    }

    const now = Date.now();
    if (now > session.expiresAt) {
      this.sessions.delete(id);
      return undefined;
    }

    session.lastAccessedAt = now;
    return session;
  }

  public deleteSession(id: string): boolean {
    return this.sessions.delete(id);
  }

  public cleanExpired(): number {
    const now = Date.now();
    let cleaned = 0;
    for (const [id, session] of this.sessions.entries()) {
      if (now > session.expiresAt) {
        this.sessions.delete(id);
        cleaned++;
      }
    }
    return cleaned;
  }

  private evictOldest(): void {
    let oldestId: string | undefined;
    let oldestAccess = Infinity;

    for (const [id, session] of this.sessions.entries()) {
      if (session.lastAccessedAt < oldestAccess) {
        oldestAccess = session.lastAccessedAt;
        oldestId = id;
      }
    }

    if (oldestId) {
      this.sessions.delete(oldestId);
    }
  }

  public clear(): void {
    this.sessions.clear();
  }

  public close(): void {
    if (this.cleanupTimer) {
      clearInterval(this.cleanupTimer);
      this.cleanupTimer = undefined;
    }
    this.sessions.clear();
  }
}
