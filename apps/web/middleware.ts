import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Guard dashboard routes: without a token cookie, redirect to /login.
export function middleware(req: NextRequest) {
  const token = req.cookies.get("token")?.value;
  if (!token) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = { matcher: ["/dashboard/:path*"] };
