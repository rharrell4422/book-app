import { LandingPage } from "@/components/landing/landing-page";

// Shown every time someone lands on the app root (see LandingPage's own
// "Enter Your Library" CTA). This route already sits inside AuthGate's
// authenticated branch (see app/providers.tsx), so it only ever renders
// once you're logged in -- no auth/routing changes were needed to gate it.
// No other route links back to "/", so this only shows up on a fresh visit
// to the root URL, not mid-session navigation.
export default function Page() {
  return <LandingPage />;
}
