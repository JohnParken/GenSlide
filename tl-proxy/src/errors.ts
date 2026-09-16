/**
 * Custom error classes for TL Proxy
 */

export class ProxyError extends Error {
  public readonly statusCode: number;
  public readonly safeMessage: string;
  public readonly details?: unknown;

  constructor(statusCode: number, safeMessage: string, details?: unknown) {
    super(safeMessage);
    this.name = this.constructor.name;
    this.statusCode = statusCode;
    this.safeMessage = safeMessage;
    this.details = details;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class ProtocolError extends ProxyError {
  constructor(safeMessage: string, statusCode = 400, details?: unknown) {
    super(statusCode, safeMessage, details);
  }
}

export class AuthError extends ProxyError {
  constructor(safeMessage = "Unauthorized", statusCode = 401) {
    super(statusCode, safeMessage);
  }
}

export class SessionError extends ProxyError {
  constructor(safeMessage = "Session not found or expired", statusCode = 404) {
    super(statusCode, safeMessage);
  }
}

export class UpstreamError extends ProxyError {
  public readonly rawError?: unknown;

  constructor(safeMessage: string, statusCode = 502, rawError?: unknown) {
    super(statusCode, safeMessage);
    this.rawError = rawError;
  }
}

export class UpstreamTimeoutError extends UpstreamError {
  constructor(safeMessage = "Upstream provider timed out", rawError?: unknown) {
    super(safeMessage, 504, rawError);
  }
}

export class PayloadTooLargeError extends ProxyError {
  constructor(safeMessage = "Payload too large", statusCode = 413) {
    super(statusCode, safeMessage);
  }
}
