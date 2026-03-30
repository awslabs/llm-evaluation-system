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
      const timestamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
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
    <div className="border-t border-claude-border bg-claude-bg p-4">
      <div className="mx-auto max-w-3xl">
        {/* Upload progress indicator */}
        {isUploading && (
          <div className="mb-3 flex items-center gap-2 rounded-lg border border-claude-border bg-claude-surface px-3 py-2">
            <svg className="h-5 w-5 animate-spin text-claude-accent" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span className="text-sm text-claude-text">Uploading files...</span>
          </div>
        )}

        <div
          className={`flex items-end space-x-4 ${isDragging ? 'rounded-lg ring-2 ring-claude-accent ring-offset-2' : ''}`}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          {/* Hidden file input - accepts multiple files and various types */}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".csv,.json,.jsonl,.pdf,.txt,.md,.png,.jpg,.jpeg,.gif,.webp"
            onChange={handleFileSelect}
            className="hidden"
          />

          {/* File upload button */}
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={inputDisabled}
            className="rounded-lg border border-claude-border bg-claude-surface p-3 text-claude-muted hover:bg-claude-border hover:text-claude-text disabled:opacity-50 disabled:cursor-not-allowed"
            title="Upload files (PDF, CSV, TXT, images)"
          >
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
            </svg>
          </button>

          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={inputDisabled}
            placeholder={isDragging ? "Drop files here..." : "Type your message or drop files..."}
            className={`flex-1 resize-none rounded-lg border bg-claude-surface px-4 py-3 text-claude-text placeholder-claude-muted focus:border-claude-accent focus:outline-none focus:ring-2 focus:ring-claude-accent disabled:opacity-50 ${
              isDragging ? 'border-claude-accent' : 'border-claude-border'
            }`}
            rows={3}
          />
          {isStreaming ? (
            <button
              onClick={onCancel}
              className="rounded-lg bg-claude-accent px-6 py-3 font-semibold text-white hover:bg-claude-hover"
            >
              Stop
            </button>
          ) : (
            <button
              onClick={onSend}
              disabled={!canSend}
              className="rounded-lg bg-claude-accent px-6 py-3 font-semibold text-white hover:bg-claude-hover disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Send
            </button>
          )}
        </div>

        {/* Drop hint */}
        {isDragging && (
          <div className="mt-2 text-center text-sm text-claude-accent">
            Drop your files here (PDF, CSV, TXT, images)
          </div>
        )}
      </div>
    </div>
  );
}
