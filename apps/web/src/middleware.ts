import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";


// Define the routes that strictly require authentication
const isProtectedRoute = createRouteMatcher([
  '/dashboard(.*)',
  '/api/(.*)'
]);

export default clerkMiddleware((auth: any, req: any) => {
  // If the user navigates to a protected route without a valid session,
  // Clerk will automatically redirect them to the configured /sign-in page
  if (isProtectedRoute(req)) {
    auth().protect();
  }
});

// Configure the Matcher to optimize execution
export const config = {
  matcher: [
    // Skip Next.js internals and all static files, unless found in search params
    '/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)',
    // Always run for API routes
    '/(api|trpc)(.*)',
  ],
};
