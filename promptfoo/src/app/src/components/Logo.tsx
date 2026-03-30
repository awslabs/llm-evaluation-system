import Box from '@mui/material/Box';
import { styled } from '@mui/material/styles';
import Typography from '@mui/material/Typography';
import { Link } from 'react-router-dom';

const LogoWrapper = styled(Box)(({ theme }) => ({
  display: 'inline-flex',
  alignItems: 'center',
  padding: theme.spacing(1, 2),
}));

const LogoText = styled(Typography)(({ theme }) => ({
  fontFamily: '"Inter", sans-serif',
  fontWeight: 600,
  fontSize: '1rem',
  color: theme.palette.text.primary,
  letterSpacing: '0.02em',
}));

export default function Logo() {
  return (
    <Link to="/" style={{ textDecoration: 'none' }}>
      <LogoWrapper>
        <LogoText variant="h1">promptfoo</LogoText>
      </LogoWrapper>
    </Link>
  );
}
