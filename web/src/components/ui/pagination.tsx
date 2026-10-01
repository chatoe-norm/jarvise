import type { ButtonHTMLAttributes, ComponentProps } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export function Pagination({
  className,
  ...props
}: ComponentProps<"nav">) {
  return (
    <nav
      role="navigation"
      aria-label="pagination"
      className={cn("mx-auto flex w-full justify-center", className)}
      {...props}
    />
  );
}

export function PaginationContent({
  className,
  ...props
}: ComponentProps<"ul">) {
  return (
    <ul className={cn("flex flex-row items-center gap-1", className)} {...props} />
  );
}

export function PaginationItem({
  className,
  ...props
}: ComponentProps<"li">) {
  return <li className={cn("", className)} {...props} />;
}

type PaginationLinkProps = {
  isActive?: boolean;
} & ButtonHTMLAttributes<HTMLButtonElement>;

export function PaginationLink({
  className,
  isActive,
  ...props
}: PaginationLinkProps) {
  return (
    <Button
      type="button"
      variant={isActive ? "outline" : "ghost"}
      size="sm"
      className={cn("min-w-8", className)}
      aria-current={isActive ? "page" : undefined}
      {...props}
    />
  );
}

export function PaginationPrevious({
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={cn("gap-1 px-2.5", className)}
      {...props}
    >
      Previous
    </Button>
  );
}

export function PaginationNext({
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={cn("gap-1 px-2.5", className)}
      {...props}
    >
      Next
    </Button>
  );
}
