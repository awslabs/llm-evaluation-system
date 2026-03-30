export type CloudConfigData = {
  appUrl: string;
  isEnabled: boolean;
};

/**
 * Returns cloud config. For self-hosted deployments, cloud is always disabled.
 * No API call - returns static disabled config immediately.
 */
export default function useCloudConfig(): {
  data: CloudConfigData | null;
  isLoading: boolean;
  error: string | null;
  refetch: () => void;
} {
  return {
    data: { appUrl: '', isEnabled: false },
    isLoading: false,
    error: null,
    refetch: () => {},
  };
}
