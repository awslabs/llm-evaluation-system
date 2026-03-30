import { callApi } from '@app/utils/api';
import { useQuery } from '@tanstack/react-query';

export type ApiHealthStatus = 'unknown' | 'connected' | 'blocked' | 'disabled';

interface HealthResponse {
  status: string;
  message: string;
}

export type ApiHealthResult = {
  status: ApiHealthStatus;
  message: string;
};

/**
 * Checks the health of the connection to Promptfoo Cloud.
 */
export function useApiHealth() {
  return useQuery<ApiHealthResult, Error>({
    queryKey: ['apiHealth'],
    queryFn: async () => {
      try {
        const response = await callApi('/remote-health', { cache: 'no-store' });
        const { status, message } = (await response.json()) as HealthResponse;
        return {
          status: status === 'DISABLED' ? 'disabled' : status === 'OK' ? 'connected' : 'blocked',
          message,
        };
      } catch {
        return {
          status: 'blocked',
          message: 'Network error: Unable to check API health',
        };
      }
    },
    retry: false,
    staleTime: Infinity, // Data never goes stale - one check is enough
    initialData: {
      status: 'unknown',
      message: '',
    },
  });
}
