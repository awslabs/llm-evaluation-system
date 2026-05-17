"use client";

import { useRef, useEffect, useState, useCallback } from "react";

// Supported file types (must match backend SUPPORTED_DOCUMENT_TYPES)
const SUPPORTED_EXTENSIONS = ["csv", "json", "jsonl", "pdf", "txt", "md", "png", "jpg", "jpeg", "gif", "webp"];

// MIME type mapping
const MIME_TYPES: Record<string, string> = {
  csv: "text/csv",
  json: "application/json",
  jsonl: "application/jsonlines",
  pdf: "application/pdf",
  txt: "text/plain",
  md: "text/markdown",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
};

export interface CsvResult {
  filename: string;
  success: boolean;
  message: string;
  path?: string;
  rows_saved?: number;
  error?: string;
}

export interface UploadResult {
  success: boolean;
  folder: string | null;
  files: string[];
  count: number;
  error?: string;
  s3_keys?: string[];
  csv_results?: CsvResult[] | null;
  non_csv_files?: string[] | null;
}

interface PresignResponse {
  upload_url: string;
  s3_key: string;
  bucket: string;
  expires_in: number;
}

interface MessageInputProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onCancel: () => void;
  disabled: boolean;
  isStreaming: boolean;
  onDocumentsUploaded: (result: UploadResult) => void;
}

export default function MessageInput({
  value,
  onChange,
  onSend,
  onCancel,
  disabled,
  isStreaming,
  onDocumentsUploaded,
}: MessageInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);

  // Focus textarea when disabled changes from true to false
  useEffect(() => {
    if (!disabled && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [disabled]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  };

  // Direct upload to backend (for local dev)
  const uploadDirect = useCallback(async (fileArray: File[]) => {
    const formData = new FormData();
    fileArray.forEach(file => formData.append("files", file));

    const response = await fetch("/api/documents/upload", {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || "Upload failed");
    }

    const result: UploadResult = await response.json();
    onDocumentsUploaded(result);
  }, [onDocumentsUploaded]);

  // S3 presigned URL upload flow (for AWS)
  const uploadViaS3 = useCallback(async (fileArray: File[]) => {
    // Generate folder name for multiple files
    let folder: string | null = null;
    if (fileArray.length > 1) {
      const firstFileName = fileArray[0].name;
      const nameWithoutExt = firstFileName.includes(".")
        ? firstFileName.substring(0, firstFileName.lastIndexOf("."))
        : firstFileName;
      const safeName = nameWithoutExt.replace(/[^a-zA-Z0-9-_]/g, "_");
      // Strip hyphens, colons, and the ISO "T" separator from the timestamp.
      // Build the regex at runtime so the pattern isn't a string literal that
      // Tailwind's JIT content scanner would match as a class name.
      const stripChars = "-" + ":" + "T";
      const timestamp = new Date().toISOString().slice(0, 16).replace(new RegExp("[" + stripChars + "]", "g"), "");
      folder = `${safeName}_${timestamp}`;
    }

    const s3Keys: string[] = [];

    // Upload each file
    for (const file of fileArray) {
      const ext = file.name.toLowerCase().split(".").pop() || "";
      const contentType = MIME_TYPES[ext] || "application/octet-stream";

      // Get presigned URL
      const presignResponse = await fetch("/api/documents/presign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: file.name,
          content_type: contentType,
          folder,
        }),
      });

      if (!presignResponse.ok) {
        const error = await presignResponse.json();
        throw new Error(error.detail || "Failed to get upload URL");
      }

      const presignData: PresignResponse = await presignResponse.json();

      // Upload directly to S3
      const uploadResponse = await fetch(presignData.upload_url, {
        method: "PUT",
        body: file,
        headers: {
          "Content-Type": contentType,
        },
      });

      if (!uploadResponse.ok) {
        throw new Error(`Failed to upload ${file.name} to S3`);
      }

      s3Keys.push(presignData.s3_key);
    }

    // Confirm uploads with backend
    const confirmResponse = await fetch("/api/documents/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        s3_keys: s3Keys,
        folder,
      }),
    });

    if (!confirmResponse.ok) {
      const error = await confirmResponse.json();
      throw new Error(error.detail || "Failed to confirm upload");
    }

    const result: UploadResult = await confirmResponse.json();
    onDocumentsUploaded(result);
  }, [onDocumentsUploaded]);

  const uploadFiles = useCallback(async (files: FileList | File[]) => {
    const fileArray = Array.from(files);
    if (fileArray.length === 0) return;

    // Validate file types
    const invalidFiles = fileArray.filter(file => {
      const ext = file.name.toLowerCase().split(".").pop() || "";
      return !SUPPORTED_EXTENSIONS.includes(ext);
    });

    if (invalidFiles.length > 0) {
      alert(`Unsupported file types: ${invalidFiles.map(f => f.name).join(", ")}\n\nSupported: ${SUPPORTED_EXTENSIONS.join(", ")}`);
      return;
    }

    setIsUploading(true);

    try {
      // Check upload mode (S3 vs direct)
      const modeResponse = await fetch("/api/documents/upload-mode");
      const { mode } = await modeResponse.json();

      if (mode === "s3") {
        // S3 presigned URL upload flow
        await uploadViaS3(fileArray);
      } else {
        // Direct upload flow (local dev or S3 not configured)
        await uploadDirect(fileArray);
      }
    } catch (error) {
      console.error("Upload error:", error);
      onDocumentsUploaded({
        success: false,
        folder: null,
        files: [],
        count: 0,
        error: error instanceof Error ? error.message : "Upload failed",
      });
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }, [onDocumentsUploaded, uploadDirect, uploadViaS3]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      uploadFiles(e.target.files);
    }
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);

    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      uploadFiles(e.dataTransfer.files);
    }
  };

  const canSend = !disabled && !isUploading && value.trim();
  const inputDisabled = disabled || isUploading;

  return (
    <div className="border-t border-rule bg-ink px-6 py-4">
      <div className="mx-auto max-w-3xl">
        {isUploading && (
          <div className="mb-3 flex items-center gap-3 border border-rule bg-ink-elev px-3 py-2">
            <span className="cursor-block bg-ember" />
            <span className="font-mono text-[11px] uppercase tracking-eyebrow text-bone-dim">
              Uploading files
            </span>
          </div>
        )}

        <div
          className={`flex items-end gap-3 border bg-ink-elev transition-colors ${
            isDragging ? "border-ember" : "border-rule focus-within:border-bone-mute"
          }`}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".csv,.json,.jsonl,.pdf,.txt,.md,.png,.jpg,.jpeg,.gif,.webp"
            onChange={handleFileSelect}
            className="hidden"
          />

          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={inputDisabled}
            title="Attach files (PDF, CSV, TXT, images)"
            className="m-2 flex h-9 w-9 items-center justify-center border border-rule text-bone-dim transition-colors hover:border-bone-mute hover:text-bone disabled:cursor-not-allowed disabled:opacity-40"
          >
            <svg
              className="h-4 w-4"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.6}
                d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"
              />
            </svg>
          </button>

          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={inputDisabled}
            placeholder={
              isDragging
                ? "Drop files to attach…"
                : "Compose — describe an evaluation, ask a question, drop a CSV…"
            }
            className="flex-1 resize-none bg-transparent px-1 py-3 text-[0.95rem] leading-relaxed text-bone placeholder:text-bone-mute focus:outline-none disabled:opacity-50"
            rows={3}
          />

          {isStreaming ? (
            <button
              onClick={onCancel}
              className="m-2 inline-flex items-center gap-2 border border-ember bg-ember-soft px-4 py-2 font-mono text-[11px] uppercase tracking-eyebrow text-ember transition-colors hover:bg-ember hover:text-ink"
            >
              <span className="block h-2 w-2 bg-ember" />
              Stop
            </button>
          ) : (
            <button
              onClick={onSend}
              disabled={!canSend}
              className="m-2 inline-flex items-center gap-2 border border-bone px-4 py-2 font-mono text-[11px] uppercase tracking-eyebrow text-bone transition-colors hover:bg-bone hover:text-ink disabled:cursor-not-allowed disabled:border-rule disabled:text-bone-mute disabled:hover:bg-transparent disabled:hover:text-bone-mute"
            >
              Send
              <span className="font-mono">↵</span>
            </button>
          )}
        </div>

        <div className="mt-2 flex items-center justify-between font-mono text-[10px] uppercase tracking-eyebrow">
          <span className="text-bone-mute">
            {isDragging
              ? "Release to attach — PDF, CSV, TXT, MD, JSON, image"
              : "Enter to send · Shift+Enter for newline"}
          </span>
          <span className="text-bone-mute">
            {value.length > 0 && `${value.length} chars`}
          </span>
        </div>
      </div>
    </div>
  );
}
