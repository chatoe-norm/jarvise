import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-md text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default:
          "bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:opacity-90",
        danger: "bg-[var(--color-danger)] text-white hover:opacity-90",
        ok: "bg-[var(--color-ok)] text-[var(--color-accent-foreground)] hover:opacity-90",
        outline:
          "border border-[var(--color-border)] bg-transparent text-[var(--color-foreground)] hover:bg-[var(--color-card)]",
        ghost: "hover:bg-[var(--color-card)] text-[var(--color-muted)]",
      },
      size: {
        default: "h-9 px-3 py-2 pointer-coarse:min-h-11 pointer-coarse:min-w-11",
        // 44px on phones and on any coarse pointer (tablets). Fine pointers stay compact.
        sm: "h-11 px-3 text-xs sm:h-8 sm:px-2.5 pointer-coarse:min-h-11 pointer-coarse:min-w-11",
        lg: "h-10 px-4",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  ),
);
Button.displayName = "Button";
